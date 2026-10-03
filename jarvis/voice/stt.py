"""Speech-to-text backends (section 3 / Phase 2).

Four backends, selected by configuration then availability:

* ``macos_speech``   - native, offline, via a small Swift helper using
                       Apple's Speech framework (compiled on demand).
* ``whisper_cpp``    - local, offline, if a whisper.cpp binary is on PATH.
* ``openai_whisper`` - cloud, requires JARVIS_LLM_API_KEY.
* ``transcript``     - pass-through for text input and tests.

Nothing here pretends to work. If no backend can transcribe, the result says
so explicitly.
"""

from __future__ import annotations

import asyncio
import json
import platform
import shutil
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from ..config import Settings, get_settings
from ..logging_setup import get_logger

log = get_logger("voice.stt")

NATIVE_DIR = Path(__file__).resolve().parent / "native"
SWIFT_SOURCE = NATIVE_DIR / "macos_speech.swift"


@dataclass
class STTResult:
    """Outcome of a transcription request."""

    ok: bool
    backend: str
    text: str = ""
    confidence: float = 0.0
    duration_ms: int = 0
    error: str = ""
    meta: dict = field(default_factory=dict)

    @property
    def summary(self) -> str:
        return self.text if self.ok else (self.error or "transcription failed")


class STTBackend(ABC):
    """Transcribes an audio file to text."""

    name: str = "base"
    #: False for a stand-in that echoes text a test fed it. A caller must never treat a
    #: non-real backend as ears: `TranscriptSTT` answering `ok` for a recording is how a
    #: machine with no microphone reports having heard something.
    real: bool = True

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()

    # -- what has actually been shown about this backend -------------------
    #
    # `available()` answers "may this be tried". `state` answers "what did trying it
    # last reveal". They are different questions, and merging them is what let a
    # machine that had never transcribed anything report `can_listen: true`.
    @property
    def _proven_marker(self) -> Path:
        """Written only by a transcription that returned text."""
        return self.settings.voice_path / "native" / f".proven_{self.name}"

    def _refused_here(self) -> bool:
        return False

    def _mark_proven(self) -> None:
        try:
            self._proven_marker.parent.mkdir(parents=True, exist_ok=True)
            self._proven_marker.write_text(time.strftime("%Y-%m-%dT%H:%M:%S%z\n"))
        except OSError:  # pragma: no cover - a home we cannot write is reported as untested
            pass

    @property
    def state(self) -> str:
        """``proven`` | ``refused`` | ``untested``."""
        if self._proven_marker.exists():
            return "proven"
        if self._refused_here():
            return "refused"
        return "untested"

    @property
    def can_listen(self) -> bool:
        """True once this backend has transcribed something here. Not a promise before."""
        return self.state == "proven"

    @property
    def may_try(self) -> bool:
        """Whether a client should still offer the microphone.

        ``untested`` must stay offered: the first attempt is the only thing that can
        ever produce proof. ``refused`` is not, because the reason is a permission
        this machine has already said no to.
        """
        return self.real and self.state != "refused"

    @abstractmethod
    def available(self) -> bool:
        """True when this backend can transcribe on this machine."""

    @abstractmethod
    async def transcribe(self, audio_path: Path) -> STTResult:
        """Return the transcript of ``audio_path``."""


class TranscriptSTT(STTBackend):
    """Pass-through backend: treats supplied text as the transcript.

    This is what makes the voice pipeline testable without a microphone, and
    what powers text input into the same session abstraction. It is **not** a
    backend that can hear: given a recording and nothing fed to it, it says so.
    Until that was true, a machine with no working speech backend fell through
    to this class and `/voice/transcribe` answered a real clip with
    `ok: true, confidence: 1.0, text: ""`.
    """

    name = "transcript"
    real = False

    def __init__(self, settings: Optional[Settings] = None, text: str = "") -> None:
        super().__init__(settings)
        self.text = text

    def available(self) -> bool:
        return True

    def feed(self, text: str) -> None:
        """Provide the transcript the next call should return."""
        self.text = text

    async def transcribe(self, audio_path: Path) -> STTResult:
        if not self.text:
            return STTResult(
                False, self.name, confidence=0.0,
                error=("no microphone on this machine: the transcript backend only repeats "
                       "text a test handed it, and this is an audio file"),
                meta={"source": "none", "audio": str(audio_path)},
            )
        return STTResult(
            True, self.name, text=self.text, confidence=1.0,
            meta={"source": "text", "passthrough": True},
        )


class MacOSSpeechSTT(STTBackend):
    """Native macOS transcription via a compiled Swift helper.

    The helper is built lazily with ``swiftc``. Two things are worth knowing:

    * Apple's Speech framework gates access behind TCC, and requesting
      authorisation from a bare command-line binary (no signed ``.app`` bundle)
      makes the process abort with SIGABRT. That is a platform constraint, not
      a bug here -- so this backend reports it precisely and the session falls
      back to another backend.
    * Run from a signed, bundled host with Speech Recognition granted, the same
      helper works unchanged.
    """

    name = "macos_speech"

    #: Set after the first attempt so /voice/status can report the real state.
    _runtime_note: Optional[str] = None

    def __init__(self, settings: Optional[Settings] = None) -> None:
        super().__init__(settings)
        self._binary: Optional[Path] = None
        self._runtime_note: Optional[str] = None

    @property
    def _blocked_marker(self) -> Path:
        """Marker written after TCC aborts the helper, so we stop retrying."""
        return self.settings.voice_path / "native" / ".tcc_blocked"

    def available(self) -> bool:
        # Only meaningful on macOS with a Swift toolchain present.
        if shutil.which("swiftc") is None or not SWIFT_SOURCE.exists():
            return False
        # If TCC already aborted us once, do not keep selecting this backend.
        # Delete the marker after running from a properly bundled host.
        return not self._blocked_marker.exists()

    def runtime_note(self) -> Optional[str]:
        return self._runtime_note

    def _refused_here(self) -> bool:
        return self._blocked_marker.exists()

    def _ensure_binary(self) -> Optional[Path]:
        if self._binary and self._binary.exists():
            return self._binary
        if not self.available():
            return None

        build_dir = self.settings.voice_path / "native"
        build_dir.mkdir(parents=True, exist_ok=True)
        binary = build_dir / "jarvis_speech"

        # Rebuild only when the source is newer than the binary.
        if binary.exists() and binary.stat().st_mtime >= SWIFT_SOURCE.stat().st_mtime:
            self._binary = binary
            return binary

        try:
            proc = subprocess.run(
                [
                    "swiftc", "-O",
                    # Pin the machine this runs on. Measured 2026-10-03: inherited from a
                    # translated parent, the helper came out x86_64 on an arm64 Mac and
                    # took 4m08s to build; the same source built natively took 2.2s.
                    "-target", f"{platform.machine().lower()}-apple-macosx11.0",
                    "-framework", "Speech",
                    "-framework", "Foundation",
                    "-o", str(binary),
                    str(SWIFT_SOURCE),
                ],
                capture_output=True,
                text=True,
                timeout=180,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            log.info("swift build failed", extra_fields={"error": str(exc)})
            return None

        if proc.returncode != 0 or not binary.exists():
            log.info(
                "swift build failed",
                extra_fields={"stderr": proc.stderr[-500:] if proc.stderr else ""},
            )
            return None

        self._binary = binary
        return binary

    async def transcribe(self, audio_path: Path) -> STTResult:
        started = time.perf_counter()
        binary = await asyncio.to_thread(self._ensure_binary)
        if binary is None:
            return STTResult(
                False, self.name,
                error="macOS speech helper could not be built (swiftc missing or compile error).",
            )

        proc = await asyncio.create_subprocess_exec(
            str(binary), str(audio_path), self.settings.stt_language,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        elapsed = int((time.perf_counter() - started) * 1000)
        stdout = out.decode(errors="replace").strip()

        # SIGABRT (134) means TCC aborted the process before it could answer.
        if proc.returncode in (134, -6):
            self._runtime_note = "blocked by TCC (this process was not attributable to Vantage)"
            # Remember the block so auto-selection stops picking this backend.
            try:
                self._blocked_marker.parent.mkdir(parents=True, exist_ok=True)
                self._blocked_marker.write_text(
                    "Apple's Speech framework aborted the helper because macOS would not"
                    " attribute it to a signed application with a speech usage string."
                    " This happens when Vantage was started from a terminal rather than"
                    " opened as an app. Delete this file after launching Vantage with"
                    " `open Vantage.app` (or by double-clicking it).\n"
                )
            except OSError:
                pass

            return STTResult(
                False,
                self.name,
                error=(
                    "macOS Speech aborted the helper: this process was not attributable"
                    " to the Vantage app, which happens when it is started from a"
                    " terminal instead of opened as an application. Open Vantage.app and"
                    f" try again, or configure another backend"
                    " (JARVIS_STT_BACKEND=whisper_cpp or openai_whisper)."
                ),
                duration_ms=elapsed,
            )

        try:
            payload = json.loads(stdout) if stdout else {}
        except json.JSONDecodeError:
            payload = {}

        if proc.returncode != 0 or not payload.get("ok"):
            reason = (
                payload.get("error")
                or err.decode(errors="replace").strip()
                or f"helper exited with {proc.returncode}"
            )
            self._runtime_note = reason[:120]
            return STTResult(
                False, self.name,
                error=f"macOS speech failed: {reason}",
                duration_ms=elapsed,
            )

        self._runtime_note = None
        text = payload.get("text", "")
        if text.strip():
            # The proof is a transcript. A silent "success" says nothing about ears.
            self._mark_proven()
        return STTResult(
            True, self.name,
            text=text,
            confidence=float(payload.get("confidence", 0.0)),
            duration_ms=elapsed,
        )


class WhisperCppSTT(STTBackend):
    """Local transcription via a whisper.cpp binary."""

    name = "whisper_cpp"

    BINARIES = ("whisper-cli", "whisper-cpp", "whisper", "main")

    def __init__(self, settings: Optional[Settings] = None) -> None:
        super().__init__(settings)
        self._binary = self._find_binary()

    def _find_binary(self) -> Optional[str]:
        for name in self.BINARIES:
            path = shutil.which(name)
            if path:
                return path
        return None

    def available(self) -> bool:
        return self._binary is not None

    async def transcribe(self, audio_path: Path) -> STTResult:
        started = time.perf_counter()
        model = self.settings.stt_model
        argv = [self._binary, "-f", str(audio_path), "-nt", "-otxt", "-of", str(audio_path.with_suffix(""))]
        if model and model.endswith(".bin"):
            argv += ["-m", model]

        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        elapsed = int((time.perf_counter() - started) * 1000)

        if proc.returncode != 0:
            return STTResult(
                False, self.name,
                error=f"whisper.cpp failed: {err.decode(errors='replace').strip()[:300]}",
                duration_ms=elapsed,
            )

        transcript_file = audio_path.with_suffix(".txt")
        text = ""
        if transcript_file.exists():
            text = transcript_file.read_text(errors="replace").strip()
        elif out:
            text = out.decode(errors="replace").strip()

        return STTResult(True, self.name, text=text, duration_ms=elapsed)


class OpenAIWhisperSTT(STTBackend):
    """Cloud transcription through an OpenAI-compatible audio endpoint."""

    name = "openai_whisper"

    def available(self) -> bool:
        return bool(self.settings.llm_base_url and self.settings.llm_api_key)

    async def transcribe(self, audio_path: Path) -> STTResult:
        started = time.perf_counter()
        try:
            import httpx
        except ImportError:
            return STTResult(False, self.name, error="httpx is not installed")

        url = self.settings.llm_base_url.rstrip("/") + "/audio/transcriptions"
        headers = {"Authorization": f"Bearer {self.settings.llm_api_key}"}

        try:
            with audio_path.open("rb") as handle:
                files = {"file": (audio_path.name, handle, "audio/wav")}
                data = {"model": self.settings.stt_model, "language": self.settings.stt_language[:2]}
                async with httpx.AsyncClient(timeout=60.0) as client:
                    response = await client.post(url, headers=headers, files=files, data=data)
        except Exception as exc:
            return STTResult(False, self.name, error=f"upload failed: {exc}")

        elapsed = int((time.perf_counter() - started) * 1000)
        if response.status_code != 200:
            return STTResult(
                False, self.name,
                error=f"provider returned {response.status_code}: {response.text[:200]}",
                duration_ms=elapsed,
            )
        payload = response.json()
        return STTResult(True, self.name, text=payload.get("text", ""), duration_ms=elapsed)


def get_stt(settings: Optional[Settings] = None, transcript: str = "") -> STTBackend:
    """Select an STT backend based on configuration, then availability."""
    settings = settings or get_settings()
    requested = (settings.stt_backend or "auto").lower()

    registry = {
        "macos_speech": lambda: MacOSSpeechSTT(settings),
        "whisper_cpp": lambda: WhisperCppSTT(settings),
        "openai_whisper": lambda: OpenAIWhisperSTT(settings),
        "transcript": lambda: TranscriptSTT(settings, transcript),
    }

    if requested in registry:
        backend = registry[requested]()
        if backend.available():
            return backend
        log.info("requested stt backend unavailable", extra_fields={"backend": requested})

    # Prefer local, offline, private backends before cloud.
    for name in ("macos_speech", "whisper_cpp", "openai_whisper"):
        backend = registry[name]()
        if backend.available():
            log.info("stt backend selected", extra_fields={"backend": name})
            return backend

    log.info("no stt backend available; the transcript passthrough cannot hear audio")
    return TranscriptSTT(settings, transcript)


#: What a person has to do to give this machine ears, cheapest first. Built when asked,
#: because the marker path belongs to whichever home this process was pointed at.
def ears_advice() -> str:
    """The sentence a caller shows when a transcription cannot happen."""
    marker = MacOSSpeechSTT()._blocked_marker
    return (
        "No microphone backend has transcribed anything here yet. Three ways, cheapest "
        "first: (1) run Vantage as the app it is — double-click it, or `open Vantage.app` "
        "— because a copy started from a terminal is not the process macOS will grant "
        "speech to; then allow Speech Recognition when asked, and if it was refused "
        f"before, delete {marker} to retry Apple's on-device transcription; "
        "(2) install whisper.cpp (`brew install whisper-cpp`) for offline transcription "
        "with no permission dance; (3) set a hosted key with JARVIS_LLM_API_KEY to "
        "transcribe over the network, which sends the audio off this machine."
    )


def describe_stt() -> dict:
    """Report what is *known* about each backend, not what is configured."""
    settings = get_settings()
    backends = {
        "macos_speech": MacOSSpeechSTT(settings),
        "whisper_cpp": WhisperCppSTT(settings),
        "openai_whisper": OpenAIWhisperSTT(settings),
    }
    states = {name: backend.state for name, backend in backends.items()}
    available = {name: backend.available() for name, backend in backends.items()}
    notes = []
    if states["macos_speech"] == "refused":
        notes.append(
            "macos_speech was refused by macOS: the helper was aborted before it could "
            "answer, which is what happens when Vantage runs from a terminal instead of "
            f"as an app. Delete {backends['macos_speech']._blocked_marker} after opening"
            " Vantage.app to retry."
        )

    proven = [name for name, state in states.items() if state == "proven"]
    # The aggregate is "refused" only when every candidate said no; one untested backend
    # left is a machine that has not been asked yet, which is a different sentence.
    if proven:
        aggregate = "proven"
    elif all(state == "refused" for state in states.values()):
        aggregate = "refused"
    else:
        aggregate = "untested"

    return {
        "configured_backend": settings.stt_backend,
        "language": settings.stt_language,
        #: Whether a spoken word has become text on this machine. Proven, not promised:
        #: `available()` means "may be tried", and treating that as ears is what made a
        #: fresh install report `can_listen: true` before anything had ever been sent.
        "can_listen": bool(proven),
        "state": aggregate,
        #: Whether a client should still offer its microphone. True while a backend is
        #: untested, because the first attempt is the only thing that can prove one.
        "may_try": any(b.may_try and b.available() for b in backends.values()),
        "states": states,
        "available": {**available, "transcript": True},
        "notes": notes,
        #: The reason a caller with no ears can be told. Was only reachable on a failed
        #: transcription, which left a disabled microphone button with nothing to say.
        "advice": "" if proven else ears_advice(),
    }
