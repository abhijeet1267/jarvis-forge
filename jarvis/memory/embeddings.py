"""Embeddings for semantic memory (v0.9).

**No numeric library is installed** (``numpy``, ``scipy``, ``sklearn`` and
``torch`` are all absent), and there is no embedding API key configured. So the
default embedder is a real but *lexical* vector representation built with the
standard library alone:

* :class:`HashingEmbedder` — the hashing trick over word tokens and character
  trigrams, producing a fixed-dimension signed vector.

Being honest about what this is: it is **not** a neural embedding. It captures
morphological and typographic similarity (``"running"`` ≈ ``"run"``,
``"recieve"`` ≈ ``"receive"``) and gives fuzzy matching that plain keyword
search misses — but it does not understand paraphrase. It is a genuine vector
space with a genuine cosine metric, and it needs no dependencies, no network and
no model download.

:class:`RemoteEmbedder` is the upgrade path: when a model endpoint is
configured it produces true semantic vectors, and the rest of the system does
not change.

One trap worth stating: Python's built-in ``hash()`` is **randomised per
process**, so it must never be used for this. Embeddings are persisted, and a
randomised hash would silently invalidate every stored vector on restart. All
hashing here goes through ``hashlib``.
"""

from __future__ import annotations

import hashlib
import math
import re
import struct
from abc import ABC, abstractmethod
from typing import Dict, Iterable, List, Optional, Sequence

from ..logging_setup import get_logger

log = get_logger("memory.embeddings")

#: Vector width. 256 keeps cosine search cheap in pure Python while still
#: separating documents; collisions are mitigated by the signed hash.
DEFAULT_DIM = 256

_WORD = re.compile(r"[a-z0-9]+")


def _hashed_index(feature: str, dim: int) -> tuple[int, float]:
    """Map a feature to a bucket and a sign.

    The sign is derived from a separate bit of the same digest, so two features
    colliding in a bucket are as likely to cancel as to reinforce -- which keeps
    collisions from systematically inflating similarity.
    """
    digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
    value = struct.unpack("<Q", digest)[0]
    return value % dim, 1.0 if (value >> 63) & 1 else -1.0


def features(text: str) -> Dict[str, float]:
    """Extract weighted features from text.

    Word tokens carry most of the signal; character trigrams add typo and
    morphology tolerance. Term frequency is damped sublinearly so a word
    repeated ten times does not dominate.
    """
    lowered = (text or "").lower()
    weights: Dict[str, float] = {}

    words = _WORD.findall(lowered)
    counts: Dict[str, int] = {}
    for word in words:
        counts[word] = counts.get(word, 0) + 1
    for word, count in counts.items():
        weights[f"w:{word}"] = 1.0 + math.log(count)

    # Character trigrams over the word sequence (not raw text, so punctuation
    # and spacing do not create noise features).
    joined = " ".join(words)
    for i in range(len(joined) - 2):
        trigram = joined[i : i + 3]
        weights[f"t:{trigram}"] = weights.get(f"t:{trigram}", 0.0) + 0.35

    return weights


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity of two vectors. Pure Python, no numpy."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = norm_a = norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / math.sqrt(norm_a * norm_b)


#: Text -> vector, shared by every RemoteEmbedder in the process (see its
#: __init__). Kept at module level so one call can evict deleted text for real.
_SHARED_CACHE: Dict[str, List[float]] = {}


def forget_text_everywhere(text: str) -> int:
    """Drop every in-process embedding derived from deleted text.

    Returns how many cache entries went. A user-visible "forgotten" that leaves the
    sentence resident in a cache is a claim the process cannot support.
    """
    if not text:
        return 0
    doomed = [key for key in _SHARED_CACHE if key == text or text in key]
    for key in doomed:
        del _SHARED_CACHE[key]
    return len(doomed)


class Embedder(ABC):
    """Turns text into a vector."""

    name: str = "base"
    dim: int = DEFAULT_DIM
    is_semantic: bool = False

    @abstractmethod
    def embed(self, text: str) -> List[float]:
        ...

    def embed_many(self, texts: Iterable[str]) -> List[List[float]]:
        return [self.embed(text) for text in texts]

    def forget_text(self, text: str) -> int:
        """Drop any in-process trace of deleted text. Returns how much went.

        The base contract is "nothing is kept, so there is nothing to drop". A
        delete that leaves the deleted sentence resident in a cache is not a
        deletion the user can rely on, which is why eviction is part of the
        interface rather than something each caller guesses at.
        """
        return 0

    def describe(self) -> Dict[str, object]:
        return {
            "embedder": self.name,
            "dim": self.dim,
            "semantic": self.is_semantic,
        }


class HashingEmbedder(Embedder):
    """Dependency-free lexical vectors via the hashing trick.

    Deterministic, offline, and cheap. Not a neural embedding -- see the module
    docstring for what it does and does not capture.
    """

    name = "hashing"
    is_semantic = False

    def __init__(self, dim: int = DEFAULT_DIM) -> None:
        self.dim = max(32, int(dim))

    def embed(self, text: str) -> List[float]:
        vector = [0.0] * self.dim
        for feature, weight in features(text).items():
            index, sign = _hashed_index(feature, self.dim)
            vector[index] += sign * weight

        norm = math.sqrt(sum(value * value for value in vector))
        if norm <= 0.0:
            return vector
        return [value / norm for value in vector]

    def describe(self) -> Dict[str, object]:
        base = super().describe()
        base["note"] = (
            "Lexical hashing embedder (word tokens + character trigrams). "
            "Works offline with no dependencies; captures morphology and typos, "
            "not paraphrase."
        )
        return base


class RemoteEmbedder(Embedder):
    """True semantic vectors from a model endpoint, when one is configured."""

    name = "remote"
    is_semantic = True

    def __init__(self, settings=None, dim: int = 1536) -> None:
        from ..config import get_settings

        self.settings = settings or get_settings()
        self.dim = dim
        # One embedding per text per process, shared by every instance: the vector
        # is a function of the text and the model, so a per-object cache only makes
        # the same sentence live in several places -- and then a deletion can reach
        # none of them. Shared, and :func:`forget_text_everywhere` can mean it.
        self._cache: Dict[str, List[float]] = _SHARED_CACHE

    def available(self) -> bool:
        return bool(self.settings.llm_api_key and self.settings.llm_base_url)

    def forget_text(self, text: str) -> int:
        """Evict cache entries for deleted text.

        The cache is keyed by the raw string, so an embedding outlives the row it
        came from unless deletion says otherwise. Substring matches go too: a longer
        prompt that contains the forgotten sentence still reproduces it.
        """
        return forget_text_everywhere(text)

    def embed(self, text: str) -> List[float]:
        if text in self._cache:
            return self._cache[text]

        import json
        import urllib.request

        payload = json.dumps(
            {"model": self.settings.llm_embed_model or "text-embedding-3-small", "input": text}
        ).encode()
        request = urllib.request.Request(
            f"{self.settings.llm_base_url.rstrip('/')}/embeddings",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.settings.llm_api_key}",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read().decode())

        vector = [float(v) for v in body["data"][0]["embedding"]]
        self.dim = len(vector)
        self._cache[text] = vector
        return vector


_embedder: Optional[Embedder] = None


def get_embedder(settings=None) -> Embedder:
    """The active embedder.

    Prefers a configured model endpoint; otherwise the dependency-free local
    embedder. Never returns None -- keyword search must always have a fallback,
    but the vector layer itself always has *some* implementation available.
    """
    global _embedder
    if _embedder is not None:
        return _embedder

    remote = RemoteEmbedder(settings)
    if remote.available():
        log.info("embedder selected", extra_fields={"embedder": remote.name})
        _embedder = remote
    else:
        _embedder = HashingEmbedder()
    return _embedder


def reset_embedder() -> None:
    """Drop the cached embedder (used by tests)."""
    global _embedder
    _embedder = None


def describe_embeddings(settings=None) -> Dict[str, object]:
    remote = RemoteEmbedder(settings)
    active = get_embedder(settings)
    return {
        "active": active.name,
        "semantic": active.is_semantic,
        "dim": active.dim,
        "remote_available": remote.available(),
        "local_available": True,
    }
