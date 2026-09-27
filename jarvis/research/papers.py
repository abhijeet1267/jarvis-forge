"""Paper store (v1.0).

The rule this module exists to enforce: **JARVIS never fabricates a citation.**

Concretely:

* A paper enters the store only from an approved source backend, or because the
  user added it explicitly. There is no function that takes a title and returns
  a citation.
* Every paper records where it came from (`source_backend`) and whether that
  backend is authoritative (`verified`).
* :meth:`PaperStore.cite` is the only way to produce a citation string, and it
  only accepts ids that already exist. Ask for an id not in the store and you
  get ``None`` — not a plausible-looking invention.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..core.models import new_id, utcnow
from ..db.database import get_db
from ..logging_setup import get_logger

log = get_logger("research.papers")

#: Backends whose results are authoritative (i.e. actually fetched).
VERIFIED_BACKENDS = {"arxiv"}


@dataclass
class Paper:
    """One stored reference."""

    paper_id: str
    title: str
    authors: List[str] = field(default_factory=list)
    year: Optional[int] = None
    venue: str = ""
    url: str = ""
    doi: str = ""
    arxiv_id: str = ""
    abstract: str = ""
    source_backend: str = "user"
    verified: bool = False
    project_id: Optional[str] = None
    added_at: Optional[datetime] = None

    @property
    def first_author(self) -> str:
        return self.authors[0] if self.authors else "Unknown"

    @property
    def citation_key(self) -> str:
        """A short human-usable key, e.g. ``smith2021``."""
        surname = self.first_author.split()[-1].lower() if self.first_author else "unknown"
        surname = re.sub(r"[^a-z]", "", surname) or "unknown"
        return f"{surname}{self.year if self.year else 'nd'}"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "paper_id": self.paper_id,
            "title": self.title,
            "authors": self.authors,
            "year": self.year,
            "venue": self.venue,
            "url": self.url,
            "doi": self.doi,
            "arxiv_id": self.arxiv_id,
            "abstract": self.abstract,
            "source_backend": self.source_backend,
            "verified": self.verified,
            "project_id": self.project_id,
            "added_at": self.added_at.isoformat() if self.added_at else None,
        }


class PaperStore:
    """Persist and retrieve references."""

    def __init__(self) -> None:
        self.db = get_db()

    # -- write ---------------------------------------------------------------
    def add(
        self,
        title: str,
        *,
        authors: Optional[List[str]] = None,
        year: Optional[int] = None,
        venue: str = "",
        url: str = "",
        doi: str = "",
        arxiv_id: str = "",
        abstract: str = "",
        source_backend: str = "user",
        project_id: Optional[str] = None,
    ) -> Paper:
        """Store a reference.

        ``verified`` is derived from the backend, never passed in: only results
        fetched from an authoritative source count as verified.
        """
        title = (title or "").strip()
        if not title:
            raise ValueError("a paper needs a title")

        # De-duplicate on a stable identity where we have one.
        existing = None
        if arxiv_id:
            rows = self.db.query("SELECT * FROM papers WHERE arxiv_id = ?", (arxiv_id,))
            existing = rows[0] if rows else None
        elif doi:
            rows = self.db.query("SELECT * FROM papers WHERE doi = ?", (doi,))
            existing = rows[0] if rows else None
        if existing is None:
            rows = self.db.query("SELECT * FROM papers WHERE title = ?", (title,))
            existing = rows[0] if rows else None

        if existing is not None:
            return self._row_to_paper(existing)

        paper = Paper(
            paper_id=new_id("PAPER"),
            title=title,
            authors=authors or [],
            year=year,
            venue=venue,
            url=url,
            doi=doi,
            arxiv_id=arxiv_id,
            abstract=abstract,
            source_backend=source_backend,
            verified=source_backend in VERIFIED_BACKENDS,
            project_id=project_id,
            added_at=utcnow(),
        )

        self.db.execute(
            """
            INSERT INTO papers
                (paper_id, title, authors, year, venue, url, doi, arxiv_id,
                 abstract, source_backend, verified, project_id, added_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                paper.paper_id,
                paper.title,
                json.dumps(paper.authors),
                paper.year,
                paper.venue,
                paper.url,
                paper.doi,
                paper.arxiv_id,
                paper.abstract,
                paper.source_backend,
                1 if paper.verified else 0,
                paper.project_id,
                paper.added_at.isoformat() if paper.added_at else None,
            ),
        )
        log.info("paper stored", extra_fields={"id": paper.paper_id, "backend": source_backend})
        return paper

    def add_many(self, hits, project_id: Optional[str] = None) -> List[Paper]:
        """Store a batch of source hits, preserving their backend."""
        papers: List[Paper] = []
        for hit in hits:
            try:
                papers.append(
                    self.add(
                        hit.title,
                        authors=hit.authors,
                        year=hit.year,
                        venue=hit.venue,
                        url=hit.url,
                        doi=hit.doi,
                        arxiv_id=hit.external_id if hit.backend == "arxiv" else "",
                        abstract=hit.abstract,
                        source_backend=hit.backend or "user",
                        project_id=project_id,
                    )
                )
            except ValueError:
                continue
        return papers

    # -- read ----------------------------------------------------------------
    @staticmethod
    def _row_to_paper(row: Any) -> Paper:
        def _column(name: str, default: Any = None) -> Any:
            try:
                return row[name]
            except (IndexError, KeyError):
                return default

        added = _column("added_at")
        return Paper(
            paper_id=row["paper_id"],
            title=row["title"],
            authors=json.loads(row["authors"] or "[]"),
            year=row["year"],
            venue=row["venue"] or "",
            url=row["url"] or "",
            doi=row["doi"] or "",
            arxiv_id=row["arxiv_id"] or "",
            abstract=row["abstract"] or "",
            source_backend=row["source_backend"] or "user",
            verified=bool(row["verified"]),
            project_id=row["project_id"],
            added_at=datetime.fromisoformat(added) if added else None,
        )

    def get(self, paper_id: str) -> Optional[Paper]:
        rows = self.db.query("SELECT * FROM papers WHERE paper_id = ?", (paper_id,))
        return self._row_to_paper(rows[0]) if rows else None

    def all(self, project_id: Optional[str] = None, limit: int = 200) -> List[Paper]:
        if project_id:
            rows = self.db.query(
                "SELECT * FROM papers WHERE project_id = ? ORDER BY added_at DESC LIMIT ?",
                (project_id, limit),
            )
        else:
            rows = self.db.query(
                "SELECT * FROM papers ORDER BY added_at DESC LIMIT ?", (limit,)
            )
        return [self._row_to_paper(r) for r in rows]

    def search(self, query: str, limit: int = 10) -> List[Paper]:
        """Keyword search over stored papers (title, authors, abstract)."""
        terms = [t for t in re.findall(r"\w+", (query or "").lower()) if len(t) > 2]
        if not terms:
            return self.all(limit=limit)

        scored = []
        for paper in self.all(limit=500):
            haystack = " ".join(
                [paper.title, paper.abstract, " ".join(paper.authors), paper.venue]
            ).lower()
            score = sum(1 for term in terms if term in haystack)
            if score:
                scored.append((score, paper))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [paper for _score, paper in scored[:limit]]

    # -- citations -----------------------------------------------------------
    def cite(self, paper_id: str, style: str = "text") -> Optional[str]:
        """Produce a citation for a stored paper.

        Returns ``None`` when the paper is not in the store. That is the whole
        point: a missing reference produces nothing, never a guess.
        """
        paper = self.get(paper_id)
        if paper is None:
            return None

        year = paper.year if paper.year else "n.d."
        if style == "bibtex":
            key = paper.citation_key
            authors = " and ".join(paper.authors) if paper.authors else "Unknown"
            entry = [
                f"@misc{{{key},",
                f"  title = {{{paper.title}}},",
                f"  author = {{{authors}}},",
                f"  year = {{{year}}},",
            ]
            if paper.arxiv_id:
                entry.append(f"  eprint = {{{paper.arxiv_id}}},")
                entry.append("  archivePrefix = {arXiv},")
            if paper.url:
                entry.append(f"  url = {{{paper.url}}},")
            entry.append("}")
            return "\n".join(entry)

        who = paper.authors[0] if paper.authors else "Unknown"
        if len(paper.authors) > 1:
            who += " et al."
        parts = [f"{who} ({year}). {paper.title}."]
        if paper.venue:
            parts.append(f" {paper.venue}.")
        if paper.arxiv_id:
            parts.append(f" arXiv:{paper.arxiv_id}.")
        elif paper.doi:
            parts.append(f" doi:{paper.doi}.")
        return "".join(parts)

    def cite_many(self, paper_ids: List[str], style: str = "text") -> List[str]:
        """Citations for ids that exist; unknown ids are silently skipped.

        Callers that need to notice a missing reference should use :meth:`cite`
        per id and check for ``None``.
        """
        citations = []
        for paper_id in paper_ids:
            citation = self.cite(paper_id, style=style)
            if citation:
                citations.append(citation)
        return citations

    # -- maintenance ---------------------------------------------------------
    def link_project(self, paper_id: str, project_id: str) -> bool:
        paper = self.get(paper_id)
        if paper is None:
            return False
        self.db.execute(
            "UPDATE papers SET project_id = ? WHERE paper_id = ?", (project_id, paper_id)
        )
        return True

    def forget(self, paper_id: str) -> bool:
        paper = self.get(paper_id)
        if paper is None:
            return False
        self.db.execute("DELETE FROM papers WHERE paper_id = ?", (paper_id,))
        return True

    def stats(self) -> Dict[str, Any]:
        total = self.db.query("SELECT COUNT(*) AS n FROM papers")
        verified = self.db.query("SELECT COUNT(*) AS n FROM papers WHERE verified = 1")
        by_backend = {
            r["source_backend"]: r["n"]
            for r in self.db.query("SELECT source_backend, COUNT(*) AS n FROM papers GROUP BY source_backend")
        }
        total_n = total[0]["n"] if total else 0
        verified_n = verified[0]["n"] if verified else 0
        return {
            "papers": total_n,
            "verified": verified_n,
            "unverified": total_n - verified_n,
            "by_backend": by_backend,
        }
