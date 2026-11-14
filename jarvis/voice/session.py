"""Voice session orchestration (section 3 / Phase 2).

Drives the full voice loop:

    wake word -> listen -> transcribe -> JarvisCore.handle -> speak

Two behaviours from the plan are implemented here rather than deferred:

* **Interruption while speaking** - :meth:`VoiceSession.stop_speaking` cancels
  the current utterance so the user can barge in.
* **Context-aware follow-ups** - after a question or a pending confirmation the
  session accepts a bare "yes" / "no" without requiring the wake word again.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..config import Settings, get_settings
from ..core.models import CommandRequest, CommandResponse, PolicyDecision
from ..logging_setup import get_logger
from .audio import AudioRecorder, AudioUnavailable, describe_audio, get_recorder, temp_clip
from .stt import STTBackend, STTResult, describe_stt, get_stt
from .tts import TTSBackend, TTSResult, describe_tts, get_tts
from .wakeword import TextWakeWordDetector, WakeMatch, describe_wake

log = get_logger("voice.session")

# Affirmations/negations accepted as a follow-up to a pending confirmation.
AFFIRMATIVE = {"yes", "yeah", "yep", "yup", "confirm", "approve", "do it", "go ahead", "sure", "ok", "okay"}
NEGATIVE = {"no", "nope", "cancel", "deny", "stop", "don't", "dont", "never mind", "nevermind"}


@dataclass
class VoiceTurn:
    """Everything that happened in one conversational turn."""

    transcript: str = ""
    wake: Optional[WakeMatch] = None
    response: Optional[CommandResponse] = None
    speech: Optional[TTSResult] = None
    audio_path: Optional[Path] = None
    stt: Optional[STTResult] = None
    rejected: bool = False
    reject_reason: str = ""
    duration_ms: int = 0

    @property
    def reply_text(self) -> str:
        return self.response.reply if self.response else ""

    @property
    def speech_error(self) -> str:
        """Why the reply was not spoken, or "" when it was (or was not asked to be).

        Asking to speak and getting silence is a failure, and the caller has to be
        able to see it: a `/voice/command` with ``speak: true`` that answers
        ``spoken: false`` used to carry no explanation at all.
        """
        if self.speech is None or self.speech.ok:
            return ""
        return self.speech.error or "speech failed"

    def as_dict(self) -> dict:
        return {
            "transcript": self.transcript,
            "reply": self.reply_text,
            "intent": self.response.intent if self.response else None,
            "decision": self.response.decision.value if self.response and self.response.decision else None,
            "needs_confirmation": bool(self.response and self.response.needs_confirmation),
            "confirmation_id": self.response.confirmation_id if self.response else None,
            "spoken": bool(self.speech and self.speech.played),
            "speech_error": self.speech_error,
            "rejected": self.rejected,
            "reject_reason": self.reject_reason,
            "duration_ms": self.duration_ms,
        }


class VoiceSession:
    """Conversational voice front-end for :class:`JarvisCore`."""

    def __init__(
        self,
        core,
        *,
        recorder: Optional[AudioRecorder] = None,
        stt: Optional[STTBackend] = None,
        tts: Optional[TTSBackend] = None,
        settings: Optional[Settings] = None,
    ) -> None:
        self.core = core
        self.settings = settings or get_settings()
        self._recorder = recorder
        self._stt = stt
        self._tts = tts
        self.wake = TextWakeWordDetector(self.settings)

        # Conversational state.
        self.session_id: Optional[str] = None
        self.pending_confirmation: Optional[str] = None
        self.awaiting_followup = False
        self.last_turn: Optional[VoiceTurn] = None

    # -- lazily resolved backends -------------------------------------------
    @property
    def recorder(self) -> AudioRecorder:
        if self._recorder is None:
            self._recorder = get_recorder(self.settings)
        return self._recorder

    @property
    def stt(self) -> STTBackend:
        if self._stt is None:
            self._stt = get_stt(self.settings)
        return self._stt

    @property
    def tts(self) -> TTSBackend:
        if self._tts is None:
            self._tts = get_tts(self.settings)
        return self._tts

    # -- public API ---------------------------------------------------------
    async def process_text(self, text: str, *, speak: bool = False) -> VoiceTurn:
        """Handle text through the voice pipeline (wake word, follow-ups, TTS)."""
        started = time.perf_counter()
        turn = VoiceTurn(transcript=text)

        if not text.strip():
            turn.rejected, turn.reject_reason = True, "empty input"
            return turn

        command, match = self._resolve_wake(text, turn)
        if command is None:
            turn.duration_ms = int((time.perf_counter() - started) * 1000)
            self.last_turn = turn
            return turn

        turn.wake = match

        # A bare "yes"/"no" resolves a pending confirmation instead of being
        # sent to the planner as an unrecognised command.
        if self.pending_confirmation and self._is_short_reply(command):
            approved = self._is_affirmative(command)
            if self._is_affirmative(command) or self._is_negative(command):
                response = await self.core.resolve_confirmation(
                    self.pending_confirmation, approve=approved, session_id=self.session_id or ""
                )
                self.pending_confirmation = None
                self.awaiting_followup = False
                turn.response = response
                await self._maybe_speak(response.reply, speak, turn)
                turn.duration_ms = int((time.perf_counter() - started) * 1000)
                self.last_turn = turn
                return turn

        request = CommandRequest(text=command, source="voice", device_id="laptop_main")
        if self.session_id:
            request.session_id = self.session_id
        response = await self.core.handle(request)
        self.session_id = response.session_id
        turn.response = response

        # Track conversational state for the next turn. A pending confirmation,
        # a question, or an explicit clarification all mean JARVIS is waiting on
        # the user, so the next utterance should not need the wake word again.
        self.pending_confirmation = response.confirmation_id
        self.awaiting_followup = bool(
            response.needs_confirmation
            or response.reply.rstrip().endswith("?")
            or (response.plan is not None and response.plan.direct_reply)
        )

        await self._maybe_speak(response.reply, speak, turn)
        turn.duration_ms = int((time.perf_counter() - started) * 1000)
        self.last_turn = turn
        return turn

    async def process_audio(self, audio_path: Path, *, speak: bool = True) -> VoiceTurn:
        """Transcribe an audio file and run the resulting command."""
        started = time.perf_counter()
        result = await self.stt.transcribe(audio_path)

        if not result.ok:
            turn = VoiceTurn(audio_path=audio_path, stt=result)
            turn.rejected, turn.reject_reason = True, result.error
            turn.duration_ms = int((time.perf_counter() - started) * 1000)
            self.last_turn = turn
            return turn

        turn = await self.process_text(result.text, speak=speak)
        turn.audio_path = audio_path
        turn.stt = result
        return turn

    async def listen_and_respond(self, seconds: Optional[float] = None, *, speak: bool = True) -> VoiceTurn:
        """Record from the microphone, then transcribe and respond."""
        started = time.perf_counter()
        duration = seconds or self.settings.listen_seconds

        try:
            recorder = self.recorder
        except AudioUnavailable as exc:
            turn = VoiceTurn()
            turn.rejected, turn.reject_reason = True, str(exc)
            return turn

        clip = temp_clip(self.settings, tag="listen")
        try:
            await recorder.record(duration, clip)
        except AudioUnavailable as exc:
            turn = VoiceTurn()
            turn.rejected, turn.reject_reason = True, str(exc)
            turn.duration_ms = int((time.perf_counter() - started) * 1000)
            return turn

        return await self.process_audio(clip, speak=speak)

    async def stop_speaking(self) -> None:
        """Interrupt the current utterance (barge-in)."""
        await self.tts.stop()
        log.info("speech interrupted")

    async def speak(self, text: str) -> TTSResult:
        """Speak text directly, bypassing the command pipeline."""
        try:
            return await self.tts.speak(text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Same rule as _maybe_speak: a backend fault is a reported failure,
            # not an exception that escapes the route or the REPL.
            return TTSResult(
                False, getattr(self.tts, "name", "unknown"), text,
                error=f"speech failed: {exc}",
            )

    def reset(self) -> None:
        """Clear conversational state."""
        self.session_id = None
        self.pending_confirmation = None
        self.awaiting_followup = False
        self.last_turn = None

    def status(self) -> dict:
        """Full voice subsystem status for the dashboard."""
        audio = describe_audio()
        return {
            "enabled": self.settings.voice_enabled,
            "wake": describe_wake(self.settings),
            "tts": describe_tts(self.settings),
            "stt": describe_stt(),
            "audio": audio,
            "conversation": {
                "session_id": self.session_id,
                "pending_confirmation": self.pending_confirmation,
                "awaiting_followup": self.awaiting_followup,
            },
            "ready_for_microphone": audio["capture_available"],
        }

    # -- internals ----------------------------------------------------------
    def _resolve_wake(self, text: str, turn: VoiceTurn):
        """Return (command, match). ``command`` is None when the turn is rejected."""
        if not self.settings.require_wake_word or self.awaiting_followup:
            return text.strip(), WakeMatch(detected=True, command=text.strip(), matched_word="(followup)")

        match = self.wake.detect(text)
        if not match.detected:
            turn.rejected = True
            turn.reject_reason = f"no wake word (expected one of: {', '.join(self.wake.words)})"
            return None, match

        if not match.has_command:
            turn.rejected = True
            turn.reject_reason = "wake word heard but no command followed"
            return None, match

        return match.command, match

    @staticmethod
    def _is_short_reply(text: str) -> bool:
        return len(text.split()) <= 3

    @staticmethod
    def _is_affirmative(text: str) -> bool:
        return text.strip().lower().strip(".!?") in AFFIRMATIVE

    @staticmethod
    def _is_negative(text: str) -> bool:
        return text.strip().lower().strip(".!?") in NEGATIVE

    async def _maybe_speak(self, text: str, speak: bool, turn: VoiceTurn) -> None:
        if not speak or not text.strip():
            return
        # A backend that cannot start at all (`say` removed mid-session, a busy
        # audio device raising OSError) must not take the whole turn with it: the
        # answer was already computed and is about to be printed, and a raised
        # exception here crashed `run.py voice` on a command that had succeeded
        # and turned `/voice/command` into a 500. Speech is an extra channel;
        # its failure is reported through the turn, not by discarding the reply.
        try:
            turn.speech = await self.tts.speak(text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            turn.speech = TTSResult(
                False, getattr(self.tts, "name", "unknown"), text,
                error=f"speech failed: {exc}",
            )
            log.info("speech failed", extra_fields={"error": str(exc)})
