"""Microphone capture.

Backends are probed in order of quality/portability. If none can capture
(no microphone, no permission, no binary) the recorder raises
:class:`AudioUnavailable` with actionable guidance rather than silently
returning silence -- the plan's honesty rule applies to hardware too.

Consent is measured, not assumed: :func:`probe_mic` asks AVFoundation what the
host process is actually allowed to do. A backend being *installed* is not the
same as a microphone being *usable*, and this module never conflates them.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
import wave
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional

from ..config import Settings, get_settings
from ..logging_setup import get_logger
from ..tools.base import run_command

log = get_logger("voice.audio")

NATIVE_DIR = Path(__file__).resolve().parent / "native"
SWIFT_SOURCE = NATIVE_DIR / "mic_state.swift"

#: Building the helper is a one-off cost; reading it should stay cheap.
_COMPILE_TIMEOUT = 180.0
_PROBE_TIMEOUT = 5.0
#: The OS assesses a never-run binary; measured ~8s on first exec vs 0.15s after.
_FIRST_RUN_TIMEOUT = 45.0
#: How stale a consent reading may get before a refresh is scheduled. Consent
#: can change mid-session, so this is short -- but it keeps a dashboard polling
#: every 8s from spawning a subprocess per poll.
_REFRESH_SECONDS = 10.0
#: How long a capture attempt waits for the first measurement to land. Covers a
#: cold Swift build plus macOS's first-launch assessment of the binary.
_CAPTURE_WAIT_SECONDS = 60.0

#: permission -> what to tell a human, in the same order AVFoundation uses.
PERMISSION_HINTS = {
    "authorized": "",
    "denied": (
        "macOS has denied this app the microphone. Enable it in "
        "System Settings -> Privacy & Security -> Microphone, then restart it."
    ),
    "notDetermined": (
        "macOS has never been asked to let this app use the microphone, so "
        "capture would stall on a prompt it cannot show. Run JARVIS from a "
        "signed app bundle (for example the built desktop app) to be asked."
    ),
    "restricted": "This user account is not allowed to use the microphone at all.",
    "unknown": "",
}


def consent_host() -> str:
    """Whose consent the probe measured.

    TCC records permission per app bundle, so an answer gathered from a
    terminal is not an answer about "JARVIS" -- it is an answer about the
    terminal. Reporting the host keeps the two from being confused.
    """
    marker = ".app/Contents/"
    path = str(Path(sys.executable).resolve())
    if marker in path:
        return path[: path.index(marker) + len(".app")]
    return f"unbundled process ({Path(sys.executable).name})"


class AudioUnavailable(RuntimeError):
    """Raised when no microphone input device can be used."""


def _default_settings(settings: Optional[Settings] = None) -> Settings:
    return settings or get_settings()


class MicProbe:
    """Compiles and runs the AVFoundation consent helper."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = _default_settings(settings)
        self._binary: Optional[Path] = None
        self._first_run = False
        self._build_failure: Optional[str] = None
        self._lock = threading.Lock()
        self._value: Optional[dict] = None
        self._measured_at = 0.0
        self._refreshing = False

    @property
    def binary_path(self) -> Path:
        return self.settings.voice_path / "mic_state"

    def _build(self) -> Optional[Path]:
        """Compile the helper, or record why it cannot be built."""
        if platform.system() != "Darwin":
            self._build_failure = "Microphone consent is only readable on macOS."
            return None
        if not SWIFT_SOURCE.is_file():
            self._build_failure = "The consent helper source is missing."
            return None
        compiler = shutil.which("swiftc")
        if compiler is None:
            self._build_failure = "swiftc is not installed, so the consent helper cannot be built."
            return None

        target = self.binary_path
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".new")
        try:
            completed = subprocess.run(
                [compiler, "-O", str(SWIFT_SOURCE), "-o", str(temporary)],
                capture_output=True,
                text=True,
                timeout=_COMPILE_TIMEOUT,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            self._build_failure = f"building the consent helper failed: {exc}"
            temporary.unlink(missing_ok=True)
            return None

        if completed.returncode != 0 or not temporary.exists():
            detail = (completed.stderr or "").strip().splitlines()
            self._build_failure = "building the consent helper failed: " + (
                detail[0][:120] if detail else "unknown error"
            )
            temporary.unlink(missing_ok=True)
            return None

        temporary.chmod(0o755)
        os.replace(temporary, target)
        log.info("mic consent helper built", extra_fields={"path": str(target)})
        return target

    def read(self, *, wait: float = 0.0) -> dict:
        """Return the consent state without blocking the caller.

        Measuring can be slow -- the first build compiles Swift, and macOS
        spends seconds assessing a binary it has not run before. A status
        endpoint cannot afford that: the desktop app's own health check reads
        ``/voice/status`` with a 3-second client timeout, and a synchronous
        probe made it time out on every fresh install.

        So the slow part runs on a background thread. Callers get the last
        measurement with its age, or an explicit "still measuring" -- never a
        guessed permission.

        ``wait`` is for the capture path. Recording is a human action that can
        afford a bounded pause, and answering "unknown" there would let ffmpeg
        spawn and hang for far longer than this does.
        """
        thread: Optional[threading.Thread] = None
        with self._lock:
            stale = self._value is None or (
                time.monotonic() - self._measured_at
            ) >= _REFRESH_SECONDS
            if stale and not self._refreshing:
                self._refreshing = True
                thread = threading.Thread(target=self._refresh, daemon=True)
                thread.start()

        if thread is not None and wait > 0:
            thread.join(timeout=wait)

        with self._lock:
            value = self._value
            age = None if value is None else round(time.monotonic() - self._measured_at, 2)

        if value is None:
            # Only reachable while the very first measurement is in flight:
            # _measure() always returns a dict, so a finished refresh sets it.
            return {
                "known": False,
                "pending": True,
                "permission": "unknown",
                "devices": [],
                "reason": "measuring microphone consent for the first time",
                "age_seconds": None,
            }
        return {**value, "age_seconds": age}

    def _refresh(self) -> None:
        try:
            measured = self._measure()
        finally:
            with self._lock:
                self._refreshing = False
        with self._lock:
            self._value = measured
            self._measured_at = time.monotonic()

    def _measure(self) -> dict:
        """Ask AVFoundation, the slow way. Runs on the refresh thread."""
        if self._binary is None:
            cached = self.binary_path
            if cached.exists():
                self._binary = cached
            else:
                self._binary = self._build()
                # A binary that has never run is assessed by macOS on its first
                # exec, which measured ~8s against 0.15s afterwards. Timing out
                # there would report "consent unreadable" on a machine that is
                # fine, so the first run gets the patience the OS demands.
                self._first_run = self._binary is not None
        if self._binary is None:
            return {
                "known": False,
                "permission": "unknown",
                "devices": [],
                "reason": self._build_failure or "Microphone consent could not be read.",
            }

        timeout = _FIRST_RUN_TIMEOUT if self._first_run else _PROBE_TIMEOUT
        self._first_run = False
        try:
            completed = subprocess.run(
                [str(self._binary)], capture_output=True, text=True, timeout=timeout
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return {
                "known": False,
                "permission": "unknown",
                "devices": [],
                "reason": f"reading microphone consent did not complete: {exc}",
            }

        if completed.returncode != 0:
            return {
                "known": False,
                "permission": "unknown",
                "devices": [],
                "reason": f"the consent helper failed: {(completed.stderr or '').strip()[:120]}",
            }

        try:
            payload = json.loads(completed.stdout or "{}")
        except json.JSONDecodeError:
            return {
                "known": False,
                "permission": "unknown",
                "devices": [],
                "reason": "the consent helper returned unreadable output.",
            }

        permission = str(payload.get("permission") or "unknown")
        return {
            "known": permission in PERMISSION_HINTS,
            "permission": permission,
            "devices": [
                {"name": str(d.get("name", "")), "in_use": bool(d.get("in_use"))}
                for d in payload.get("devices", [])
                if isinstance(d, dict)
            ],
            "reason": "",
        }


_probe: Optional[MicProbe] = None


def get_mic_probe(settings: Optional[Settings] = None) -> MicProbe:
    """The shared probe (the helper is built once and reused)."""
    global _probe
    if _probe is None:
        _probe = MicProbe(settings)
    return _probe


def reset_mic_probe() -> None:
    """Drop the cached helper. Used by tests that point JARVIS_HOME elsewhere."""
    global _probe
    _probe = None


def probe_mic(settings: Optional[Settings] = None, *, wait: float = 0.0) -> dict:
    """What macOS currently allows this process to hear.

    Returns at once unless ``wait`` asks it to afford a first measurement.
    """
    return get_mic_probe(settings).read(wait=wait)


def microphone_state(settings: Optional[Settings] = None, *, wait: float = 0.0) -> dict:
    """Consent summary shaped for status endpoints.

    Four outcomes, never collapsed: granted, blocked (with evidence), measuring
    (an answer is on its way), and unknown (this machine cannot answer at all).
    The last two read the same a second ago, which told a user their microphone
    was unreadable while the probe was merely still working.
    """
    probe = probe_mic(settings, wait=wait)
    permission = probe["permission"]
    if probe.get("pending"):
        state, detail = "measuring", probe["reason"]
    elif not probe["known"]:
        state, detail = "unknown", probe["reason"]
    elif permission == "authorized":
        state, detail = "granted", ""
    else:
        state, detail = "blocked", PERMISSION_HINTS.get(permission, "")
    return {
        "state": state,
        "permission": permission,
        "granted": state == "granted",
        "known": probe["known"],
        "detail": detail,
        "host": consent_host(),
        "measured_seconds_ago": probe.get("age_seconds"),
        "devices": probe["devices"],
    }


class AudioRecorder(ABC):
    """Captures a fixed-length clip of microphone audio."""

    name: str = "base"

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()

    @abstractmethod
    def available(self) -> bool:
        """True when this backend is *installed*. Say ``blocked`` for usable."""

    def blocked(self) -> str:
        """Why this backend cannot record right now, or "" if it can try.

        Unknown consent does not block: not measuring is not evidence of a
        fault, and refusing to try would break capture on machines where the
        probe simply cannot run.

        This call *waits* for a first measurement, though. Probing is
        asynchronous so that status polling stays fast, and a capture attempt
        that raced the first probe would spawn ffmpeg and hang for its full
        timeout -- the exact failure this module exists to prevent.
        """
        state = microphone_state(self.settings, wait=_CAPTURE_WAIT_SECONDS)
        return state["detail"] if state["state"] == "blocked" else ""

    @abstractmethod
    async def record(self, seconds: float, destination: Path) -> Path:
        """Record ``seconds`` of audio to ``destination`` (16-bit PCM WAV)."""


_installed: dict = {}


def _is_installed(key: str, detect) -> bool:
    """Remember a backend-detection result for the life of the process.

    A failed ``import sounddevice`` is not cached by Python, so re-detecting on
    every status poll made each check re-walk every sys.path entry -- about a
    second, in a health endpoint. Installing a package underneath a running
    process is not a case worth paying for.
    """
    if key not in _installed:
        _installed[key] = bool(detect())
    return _installed[key]


def reset_install_cache() -> None:
    """Forget cached backend detection. Used by tests that change the path."""
    _installed.clear()


class FfmpegRecorder(AudioRecorder):
    """Records via ffmpeg's AVFoundation input.

    Requires microphone consent for the host process. On macOS the device
    string is ``":<index>"`` -- ``":1"`` is typically the built-in microphone.
    Without consent AVFoundation does not error: the capture call waits for a
    prompt that a command-line process cannot show, so this backend checks
    consent first instead of blocking for the full timeout.
    """

    name = "ffmpeg"

    def available(self) -> bool:
        return _is_installed("ffmpeg", lambda: shutil.which("ffmpeg") is not None)

    async def record(self, seconds: float, destination: Path) -> Path:
        blocked = self.blocked()
        if blocked:
            raise AudioUnavailable(blocked)

        destination.parent.mkdir(parents=True, exist_ok=True)
        argv = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-f", "avfoundation",
            "-i", self.settings.audio_device,
            "-t", str(seconds),
            "-ac", "1",
            "-ar", str(self.settings.sample_rate),
            "-y",
            str(destination),
        ]
        code, _out, err = await run_command(argv, timeout=seconds + 15)

        if code != 0 or not destination.exists() or destination.stat().st_size == 0:
            raise AudioUnavailable(
                "Microphone capture produced no audio. ffmpeg said: "
                f"{err or 'no output'} (consent: "
                f"{microphone_state(self.settings)['permission']})"
            )
        return destination


class SoundDeviceRecorder(AudioRecorder):
    """Records via the ``sounddevice`` package when it is installed."""

    name = "sounddevice"

    def available(self) -> bool:
        def detect() -> bool:
            try:
                import numpy  # noqa: F401
                import sounddevice  # noqa: F401
            except ImportError:
                return False
            return True

        return _is_installed("sounddevice", detect)

    async def record(self, seconds: float, destination: Path) -> Path:
        blocked = self.blocked()
        if blocked:
            raise AudioUnavailable(blocked)
        # sounddevice is synchronous; run it in a worker thread.
        return await asyncio.to_thread(self._record_blocking, seconds, destination)

    def _record_blocking(self, seconds: float, destination: Path) -> Path:
        import sounddevice as sd  # type: ignore
        import numpy as np  # type: ignore

        destination.parent.mkdir(parents=True, exist_ok=True)
        rate = self.settings.sample_rate
        try:
            frames = sd.rec(int(seconds * rate), samplerate=rate, channels=1, dtype="int16")
            sd.wait()
        except Exception as exc:
            raise AudioUnavailable(f"Microphone capture failed: {exc}") from exc

        with wave.open(str(destination), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            handle.writeframes(np.asarray(frames).tobytes())
        return destination


def get_recorder(settings: Optional[Settings] = None) -> AudioRecorder:
    """Return the best available recorder, or raise :class:`AudioUnavailable`."""
    settings = settings or get_settings()
    candidates: List[AudioRecorder] = [
        SoundDeviceRecorder(settings),
        FfmpegRecorder(settings),
    ]
    for recorder in candidates:
        if recorder.available():
            log.info("audio recorder selected", extra_fields={"backend": recorder.name})
            return recorder
    raise AudioUnavailable(
        "No microphone backend is available. Install ffmpeg (recommended) or "
        "`pip install sounddevice numpy`."
    )


def describe_audio(settings: Optional[Settings] = None) -> dict:
    """Report capture truth without raising (used by /voice/status).

    ``capture_available`` means *this machine can record right now*: a backend
    is installed and macOS has granted the microphone. It is deliberately
    stricter than "ffmpeg exists", which reported a working microphone on a
    machine that would stall on the first capture.
    """
    settings = settings or get_settings()
    recorders = []
    for recorder in (SoundDeviceRecorder(settings), FfmpegRecorder(settings)):
        recorders.append({"name": recorder.name, "available": recorder.available()})

    mic = microphone_state(settings)
    installed = any(r["available"] for r in recorders)
    return {
        "capture_available": bool(installed and mic["granted"]),
        "capture_state": (
            "ready" if (installed and mic["granted"])
            else "no_backend" if not installed
            else mic["state"]
        ),
        "microphone": mic,
        "consent": mic["permission"],
        "consent_detail": mic["detail"],
        "recorders": recorders,
        "device": settings.audio_device,
        "sample_rate": settings.sample_rate,
    }


def wav_duration(path: Path) -> float:
    """Duration of a WAV file in seconds (0.0 if unreadable)."""
    try:
        with wave.open(str(path), "rb") as handle:
            rate = handle.getframerate() or 1
            return handle.getnframes() / rate
    except (wave.Error, OSError):
        return 0.0


def temp_clip(settings: Optional[Settings] = None, tag: str = "clip") -> Path:
    """Path for a new temporary audio clip."""
    settings = settings or get_settings()
    directory = settings.voice_path / "captures"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{tag}_{int(time.time() * 1000)}.wav"
