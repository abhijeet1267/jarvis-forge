"""Text-to-speech backends (section 3 / Phase 2).

macOS ``say`` is a real, fully working backend here: it synthesises to a file
or speaks directly, supports 180+ voices, adjustable rate, and can be
interrupted mid-utterance -- which is what "interruption while speaking"
requires.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from ..config import Settings, get_settings
from ..logging_setup import get_logger

log = get_logger("voice.tts")


@dataclass
class TTSResult:
    """Outcome of a synthesis/playback request."""

    ok: bool
    backend: str
    text: str
    audio_path: Optional[Path] = None
    played: bool = False
    interrupted: bool = False
    duration_ms: int = 0
    error: str = ""
    meta: dict = field(default_factory=dict)

    @property
    def summary(self) -> str:
        if not self.ok:
            return self.error or "speech failed"
        if self.interrupted:
            return "speech interrupted"
        return "spoken" if self.played else "synthesised"


class TTSBackend(ABC):
    """Synthesises and optionally plays speech."""

    name: str = "base"

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()
        self._process: Optional[asyncio.subprocess.Process] = None
        self._interrupted = False

    @abstractmethod
    def available(self) -> bool:
        """True when this backend can synthesise on this machine."""

    @abstractmethod
    async def synthesize(self, text: str, destination: Optional[Path] = None) -> TTSResult:
        """Render ``text`` to audio. Does not play it."""

    async def speak(self, text: str, destination: Optional[Path] = None) -> TTSResult:
        """Synthesise and play. Backends may override to stream directly."""
        result = await self.synthesize(text, destination)
        if not result.ok or result.audio_path is None:
            return result
        played = await self.play(result.audio_path)
        result.played = played
        return result

    async def play(self, audio_path: Path) -> bool:
        """Play an audio file, blocking until finished or interrupted."""
        if not shutil.which("afplay"):
            return False
        self._interrupted = False
        self._process = await asyncio.create_subprocess_exec(
            "afplay", str(audio_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await self._process.wait()
        except asyncio.CancelledError:
            await self.stop()
            raise
        finally:
            self._process = None
        return not self._interrupted

    async def stop(self) -> None:
        """Interrupt current playback (used for barge-in)."""
        self._interrupted = True
        process = self._process
        if process and process.returncode is None:
            try:
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except (ProcessLookupError, asyncio.TimeoutError):
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
        self._process = None


class MacOSSayTTS(TTSBackend):
    """Native macOS speech synthesis via ``say``."""

    name = "macos_say"

    def available(self) -> bool:
        return shutil.which("say") is not None

    async def synthesize(self, text: str, destination: Optional[Path] = None) -> TTSResult:
        started = time.perf_counter()
        if not text.strip():
            return TTSResult(False, self.name, text, error="nothing to say")

        target = destination or self._default_path()
        target.parent.mkdir(parents=True, exist_ok=True)

        argv = ["say"]
        if self.settings.tts_voice:
            argv += ["-v", self.settings.tts_voice]
        if self.settings.tts_rate:
            argv += ["-r", str(self.settings.tts_rate)]
        argv += ["-o", str(target), text]

        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _out, err = await proc.communicate()
        elapsed = int((time.perf_counter() - started) * 1000)

        if proc.returncode != 0 or not target.exists():
            return TTSResult(
                False, self.name, text,
                error=f"say failed: {err.decode(errors='replace').strip() or 'unknown error'}",
                duration_ms=elapsed,
            )

        return TTSResult(
            True, self.name, text,
            audio_path=target,
            duration_ms=elapsed,
            meta={"bytes": target.stat().st_size, "voice": self.settings.tts_voice or "default"},
        )

    async def speak(self, text: str, destination: Optional[Path] = None) -> TTSResult:
        """Speak directly, so barge-in can terminate the process immediately."""
        started = time.perf_counter()
        if not text.strip():
            return TTSResult(False, self.name, text, error="nothing to say")

        argv = ["say"]
        if self.settings.tts_voice:
            argv += ["-v", self.settings.tts_voice]
        if self.settings.tts_rate:
            argv += ["-r", str(self.settings.tts_rate)]
        argv.append(text)

        self._interrupted = False
        self._process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _out, err = await self._process.communicate()
        code = self._process.returncode
        self._process = None
        elapsed = int((time.perf_counter() - started) * 1000)

        if self._interrupted:
            return TTSResult(
                True, self.name, text, played=True, interrupted=True,
                duration_ms=elapsed,
            )
        if code != 0:
            return TTSResult(
                False, self.name, text,
                error=f"say failed: {err.decode(errors='replace').strip()}",
                duration_ms=elapsed,
            )
        return TTSResult(True, self.name, text, played=True, duration_ms=elapsed)

    def _default_path(self) -> Path:
        directory = self.settings.voice_path / "speech"
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"speech_{int(time.time() * 1000)}.aiff"


class Pyttsx3TTS(TTSBackend):
    """Cross-platform fallback using pyttsx3 when installed."""

    name = "pyttsx3"

    def available(self) -> bool:
        try:
            import pyttsx3  # noqa: F401
        except ImportError:
            return False
        return True

    async def synthesize(self, text: str, destination: Optional[Path] = None) -> TTSResult:
        target = destination or self._default_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            await asyncio.to_thread(self._render, text, target)
        except Exception as exc:  # pragma: no cover - platform dependent
            return TTSResult(False, self.name, text, error=f"pyttsx3 failed: {exc}")
        return TTSResult(True, self.name, text, audio_path=target)

    def _render(self, text: str, target: Path) -> None:
        import pyttsx3  # type: ignore

        engine = pyttsx3.init()
        if self.settings.tts_rate:
            engine.setProperty("rate", self.settings.tts_rate)
        engine.save_to_file(text, str(target))
        engine.runAndWait()

    def _default_path(self) -> Path:
        directory = self.settings.voice_path / "speech"
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"speech_{int(time.time() * 1000)}.aiff"


class NullTTS(TTSBackend):
    """Records what would have been spoken. Used in tests and headless runs."""

    name = "null"

    def __init__(self, settings: Optional[Settings] = None) -> None:
        super().__init__(settings)
        self.spoken: List[str] = []

    def available(self) -> bool:
        return True

    async def synthesize(self, text: str, destination: Optional[Path] = None) -> TTSResult:
        self.spoken.append(text)
        return TTSResult(True, self.name, text, audio_path=None, meta={"silent": True})

    async def speak(self, text: str, destination: Optional[Path] = None) -> TTSResult:
        self.spoken.append(text)
        return TTSResult(True, self.name, text, played=True, meta={"silent": True})


def get_tts(settings: Optional[Settings] = None) -> TTSBackend:
    """Select a TTS backend based on configuration, then availability."""
    settings = settings or get_settings()
    if not settings.tts_enabled:
        return NullTTS(settings)

    requested = (settings.tts_backend or "auto").lower()
    registry = {
        "macos_say": MacOSSayTTS,
        "pyttsx3": Pyttsx3TTS,
        "null": NullTTS,
    }

    if requested in registry:
        backend = registry[requested](settings)
        if backend.available():
            return backend
        log.info("requested tts backend unavailable", extra_fields={"backend": requested})

    for name in ("macos_say", "pyttsx3"):
        backend = registry[name](settings)
        if backend.available():
            log.info("tts backend selected", extra_fields={"backend": name})
            return backend

    log.info("no tts backend available; falling back to null")
    return NullTTS(settings)


def describe_tts() -> dict:
    """Report TTS availability (used by /voice/status)."""
    settings = get_settings()
    return {
        "enabled": settings.tts_enabled,
        "configured_backend": settings.tts_backend,
        "voice": settings.tts_voice or "system default",
        "rate": settings.tts_rate,
        "available": {
            "macos_say": MacOSSayTTS(settings).available(),
            "pyttsx3": Pyttsx3TTS(settings).available(),
        },
    }
