"""Wake word detection (section 3 / Phase 2).

Two layers:

1. **Text layer** (always on) - matches the wake word in a transcript. This is
   what actually works today and is fully testable.
2. **Audio layer** (pluggable) - a continuous acoustic detector such as
   openWakeWord or Porcupine. The interface is defined so one can be dropped
   in without touching the session logic.

Transcript matching is deliberately *fuzzy*: speech recognition routinely
returns "jervis", "jarvis.", "travis" for "Jarvis", so exact matching would
make the system feel broken.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional

from ..config import Settings, get_settings
from ..logging_setup import get_logger

log = get_logger("voice.wakeword")

_NON_ALNUM = re.compile(r"[^a-z0-9\s]")
_WS = re.compile(r"\s+")
_TOKEN = re.compile(r"\S+")


@dataclass
class WakeMatch:
    """Result of scanning a transcript for the wake word."""

    detected: bool
    command: str = ""
    matched_word: str = ""
    confidence: float = 0.0

    @property
    def has_command(self) -> bool:
        return bool(self.command.strip())


def normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    return _WS.sub(" ", _NON_ALNUM.sub(" ", text.lower())).strip()


def levenshtein(a: str, b: str) -> int:
    """Edit distance between two short strings."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)

    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(
                min(
                    previous[j] + 1,        # deletion
                    current[j - 1] + 1,     # insertion
                    previous[j - 1] + (ca != cb),  # substitution
                )
            )
        previous = current
    return previous[-1]


def similarity(a: str, b: str) -> float:
    """1.0 for identical, 0.0 for completely different."""
    if not a and not b:
        return 1.0
    longest = max(len(a), len(b))
    if longest == 0:
        return 1.0
    return 1.0 - (levenshtein(a, b) / longest)


class WakeWordEngine(ABC):
    """Continuous acoustic wake-word detector (optional layer)."""

    name: str = "base"

    @abstractmethod
    def available(self) -> bool:
        """True when this engine can listen on this machine."""

    @abstractmethod
    async def wait_for_wake(self, timeout: float = 30.0) -> bool:
        """Block until the wake word is heard, or the timeout elapses."""


class TextWakeWordDetector:
    """Matches wake words inside a transcript, tolerating recognition errors."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()
        self.words: List[str] = [w.lower() for w in self.settings.wake_words] or ["jarvis"]

    def detect(self, transcript: str, tolerance: float = 0.75) -> WakeMatch:
        """Find the wake word at (or near) the start of ``transcript``.

        Matching runs on normalised tokens, but the returned command is sliced
        from the *original* tokens. Normalising the whole string would destroy
        file paths (``/tmp/a_b.txt`` -> ``tmp a b txt``), so never do that.
        """
        tokens = _TOKEN.findall(transcript)
        if not tokens:
            return WakeMatch(False)

        normalised = [normalize(token) for token in tokens]

        for word in self.words:
            word_tokens = word.split()
            span = len(word_tokens)

            for start in range(0, max(1, len(tokens) - span + 1)):
                window = " ".join(normalised[start:start + span]).strip()
                if not window:
                    continue

                score = similarity(window, word)
                if score >= tolerance:
                    # Slice the ORIGINAL tokens so case and punctuation survive.
                    remainder = " ".join(tokens[start + span:]).strip()
                    remainder = remainder.lstrip(",.:;-").strip()
                    return WakeMatch(
                        detected=True,
                        command=remainder,
                        matched_word=word,
                        confidence=score,
                    )

        return WakeMatch(False)

    def strip(self, transcript: str) -> str:
        """Return the transcript with any wake word removed."""
        match = self.detect(transcript)
        return match.command if match.detected else transcript.strip()


def get_wake_engine(settings: Optional[Settings] = None) -> Optional[WakeWordEngine]:
    """Return an acoustic wake-word engine, or None when unavailable.

    Continuous listening needs a dedicated model. Rather than ship a fake, this
    returns None and the session falls back to push-to-talk.
    """
    settings = settings or get_settings()
    for module_name, engine_name in (("openwakeword", "openwakeword"), ("pvporcupine", "porcupine")):
        try:
            __import__(module_name)
        except ImportError:
            continue
        log.info("acoustic wake engine available", extra_fields={"engine": engine_name})
        return None  # Interface reserved; no engine is wired up in v0.1.
    return None


def describe_wake(settings: Optional[Settings] = None) -> dict:
    """Report wake-word configuration (used by /voice/status)."""
    settings = settings or get_settings()
    detector = TextWakeWordDetector(settings)
    return {
        "wake_words": detector.words,
        "require_wake_word": settings.require_wake_word,
        "acoustic_engine": None,
        "mode": "transcript_matching",
    }
