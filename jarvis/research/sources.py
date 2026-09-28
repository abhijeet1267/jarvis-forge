"""Approved research sources (v1.0).

The plan requires the Research Agent to **search approved sources** and to
**never fabricate citations**. Those two requirements are the same requirement
in practice: if a paper can only enter the store by arriving from a real
backend, then every citation JARVIS emits refers to something that actually
exists.

Backends:

* :class:`ArxivSource` — the public arXiv API. No key required, and verified
  reachable from this environment. Real titles, real authors, real identifiers.
* :class:`LocalCorpusSource` — papers already in the local store, used for
  offline search and for "find work related to my project".

Anything that is not an approved backend is simply not a source. There is no
code path that invents a paper.
"""

from __future__ import annotations

import re
import urllib.parse
import time
import urllib.request
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..logging_setup import get_logger

log = get_logger("research.sources")

ATOM_NS = "{http://www.w3.org/2005/Atom}"
ARXIV_ENDPOINT = "https://export.arxiv.org/api/query"

#: Measured, not assumed: the plain-HTTP endpoint answers 301, and following that
#: redirect from urllib is what surfaced as "HTTP Error 406" in the acceptance
#: sweep -- intermittently, so a single green run proves nothing here. HTTPS
#: removes the hop; a descriptive agent is what the API asks for anyway.
#: A retry lives in the caller, not here, because a second 406 must still be
#: reported as a failure rather than retried into a fiction.
ARXIV_USER_AGENT = "JARVISForge/1.0 (+local research client; +https://github.com/abhijeet1267/jarvis-forge)"
REQUEST_TIMEOUT = 20.0


@dataclass
class SourceHit:
    """A result from a source backend, before it becomes a stored paper."""

    title: str
    authors: List[str] = field(default_factory=list)
    year: Optional[int] = None
    venue: str = ""
    url: str = ""
    doi: str = ""
    external_id: str = ""
    abstract: str = ""
    backend: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "authors": self.authors,
            "year": self.year,
            "venue": self.venue,
            "url": self.url,
            "doi": self.doi,
            "external_id": self.external_id,
            "abstract": self.abstract[:400],
            "backend": self.backend,
        }


class SourceBackend(ABC):
    """A place papers can come from."""

    name: str = "base"
    requires_network: bool = False

    @abstractmethod
    def available(self) -> bool:
        ...

    @abstractmethod
    def search(self, query: str, limit: int = 10) -> List[SourceHit]:
        ...

    def describe(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "available": self.available(),
            "requires_network": self.requires_network,
        }


# ---------------------------------------------------------------------------
# arXiv
# ---------------------------------------------------------------------------
def _text(element: Optional[ET.Element]) -> str:
    if element is None or element.text is None:
        return ""
    return " ".join(element.text.split())


def parse_arxiv(xml_text: str, limit: int) -> List[SourceHit]:
    """Parse an arXiv Atom feed into source hits."""
    hits: List[SourceHit] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        log.info("arxiv parse failed", extra_fields={"error": str(exc)})
        return []

    for entry in root.findall(f"{ATOM_NS}entry")[:limit]:
        title = _text(entry.find(f"{ATOM_NS}title"))
        if not title:
            continue

        authors = [
            _text(author.find(f"{ATOM_NS}name"))
            for author in entry.findall(f"{ATOM_NS}author")
            if _text(author.find(f"{ATOM_NS}name"))
        ]

        raw_id = _text(entry.find(f"{ATOM_NS}id"))
        arxiv_id = raw_id.rsplit("/", 1)[-1] if raw_id else ""

        published = _text(entry.find(f"{ATOM_NS}published"))[:4]
        year = int(published) if published.isdigit() else None

        # A DOI, when arXiv carries one.
        doi = ""
        for link in entry.findall(f"{ATOM_NS}link"):
            if link.get("title") == "doi":
                doi = (link.get("href") or "").replace("https://doi.org/", "")
                break

        hits.append(
            SourceHit(
                title=title,
                authors=authors,
                year=year,
                venue="arXiv preprint",
                url=raw_id or (f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else ""),
                doi=doi,
                external_id=arxiv_id,
                abstract=_text(entry.find(f"{ATOM_NS}summary")),
                backend="arxiv",
            )
        )
    return hits


class ArxivSource(SourceBackend):
    """The public arXiv API. No key needed."""

    name = "arxiv"
    requires_network = True

    def available(self) -> bool:
        return True

    def search(self, query: str, limit: int = 10) -> List[SourceHit]:
        if not query.strip():
            return []

        params = urllib.parse.urlencode(
            {
                "search_query": f"all:{query}",
                "start": 0,
                "max_results": max(1, min(int(limit), 25)),
                "sortBy": "relevance",
                "sortOrder": "descending",
            }
        )
        request = urllib.request.Request(
            f"{ARXIV_ENDPOINT}?{params}", headers={"User-Agent": ARXIV_USER_AGENT}
        )
        # Two attempts. arXiv throttles some clients briefly and refuses others
        # outright; the first case is worth one short wait, and only one, so a hard
        # refusal costs a second rather than turning into a hang.
        for attempt in range(2):
            try:
                with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                    xml_text = response.read().decode("utf-8", errors="replace")
                break
            except Exception as exc:
                if attempt:
                    # Report the failure; never substitute invented results.
                    log.info("arxiv unreachable", extra_fields={"error": str(exc)})
                    raise RuntimeError(f"arXiv could not be reached: {exc}") from exc
                time.sleep(0.8)

        return parse_arxiv(xml_text, limit)

    def describe(self) -> Dict[str, Any]:
        base = super().describe()
        base["note"] = "Public arXiv API. No key required; returns real identifiers."
        return base


# ---------------------------------------------------------------------------
# Local corpus
# ---------------------------------------------------------------------------
class LocalCorpusSource(SourceBackend):
    """Search papers already stored locally. Works offline."""

    name = "local"
    requires_network = False

    def __init__(self, search_fn=None) -> None:
        self._search_fn = search_fn

    def available(self) -> bool:
        return True

    def search(self, query: str, limit: int = 10) -> List[SourceHit]:
        from .papers import PaperStore

        hits: List[SourceHit] = []
        for paper in PaperStore().search(query, limit=limit):
            hits.append(
                SourceHit(
                    title=paper.title,
                    authors=paper.authors,
                    year=paper.year,
                    venue=paper.venue,
                    url=paper.url,
                    doi=paper.doi,
                    external_id=paper.arxiv_id or paper.paper_id,
                    abstract=paper.abstract,
                    backend="local",
                )
            )
        return hits

    def describe(self) -> Dict[str, Any]:
        base = super().describe()
        base["note"] = "Papers already in the local store. Works offline."
        return base


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
def approved_sources() -> List[SourceBackend]:
    """The sources JARVIS is allowed to search."""
    return [ArxivSource(), LocalCorpusSource()]


def get_source(name: str) -> Optional[SourceBackend]:
    for source in approved_sources():
        if source.name == name:
            return source
    return None


def describe_sources() -> Dict[str, Any]:
    return {
        "approved": [s.describe() for s in approved_sources()],
        "policy": "A paper may only enter the store from an approved backend.",
    }
