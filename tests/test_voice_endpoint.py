"""Backend voice endpoints: they must not freeze the server, and must not lie.

Two properties, each fixed once already elsewhere in this codebase:

* **The route yields.** ``/voice/status`` answers with the microphone consent
  helper's cold build, an import sweep and a ``shutil.which`` probe. On the
  event loop that stalls every other request -- the same defect
  ``/workflows/{name}/execute`` and the Google routes had.
* **Silence is not success.** A backend that records text and plays nothing
  (``NullTTS``) answers ``ok``; reporting that as a spoken reply told a person
  their machine had a voice it does not have.

``TestClient`` hides the first: it drives the app through a portal on its own
thread, so a blocking ``async def`` still leaves the client answering. The
loop-yield test calls the route's own coroutine, the pattern established in
``tests/test_r119_a_workflow_does_not_freeze_the_server.py``.
"""

from __future__ import annotations

import asyncio
import importlib
import threading

import pytest

# `jarvis.api`'s package __init__ exposes the FastAPI object as `app`, which shadows
# the module of the same name; ask for the module explicitly.
api = importlib.import_module("jarvis.api.app")
from jarvis.config import Settings
from jarvis.voice.session import VoiceSession
from jarvis.voice.stt import TranscriptSTT
from jarvis.voice.tts import NullTTS, TTSResult


def _route(path: str):
    for route in api.app.routes:
        if getattr(route, "path", None) == path:
            return route.endpoint
    raise AssertionError(f"route {path} is missing")


class _Core:
    """Minimal orchestrator stand-in: the route tests are about the voice layer."""

    async def handle(self, request):
        from jarvis.core.models import CommandResponse

        return CommandResponse(reply="it is noon", session_id="s", intent="time")

    async def resolve_confirmation(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("no confirmation is pending in these tests")


@pytest.fixture()
def silent_session() -> VoiceSession:
    """The worst case for honesty: a backend that reports success and is silent."""
    return VoiceSession(_Core(), tts=NullTTS(), stt=TranscriptSTT(text=""))


# ---------------------------------------------------------------------------
# The status route yields to the loop
# ---------------------------------------------------------------------------
def test_status_route_does_not_block_the_event_loop(
    monkeypatch: pytest.MonkeyPatch, silent_session: VoiceSession
) -> None:
    """Run the route's own coroutine beside a ticker that must keep advancing."""
    release = threading.Event()
    started = threading.Event()

    def slow_status(self):
        started.set()
        release.wait(timeout=10)  # a blocking subprocess / probe, stood in for
        return {"tts": {"can_speak": True}}

    monkeypatch.setattr(VoiceSession, "status", slow_status)
    monkeypatch.setattr(api, "get_voice", lambda: silent_session)

    async def scenario():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        task = asyncio.create_task(ticker())
        route = asyncio.create_task(_route("/voice/status")(None))

        for _ in range(500):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set(), "status() never ran"

        before = ticks
        await asyncio.sleep(0.3)
        advanced = ticks - before
        release.set()
        result = await route
        task.cancel()
        return advanced, result

    advanced, result = asyncio.run(scenario())

    assert result == {"tts": {"can_speak": True}}
    assert advanced >= 5, (
        f"the loop advanced only {advanced} times while status() blocked -- "
        "the route is holding the event loop"
    )


def test_status_route_moves_the_call_off_the_loop_structurally() -> None:
    """Cheap, exact guard against a future 'simplification' back to a direct call."""
    import inspect

    source = inspect.getsource(_route("/voice/status"))
    assert "to_thread" in source or "run_in_executor" in source, source


# ---------------------------------------------------------------------------
# The speak route tells the truth about silence
# ---------------------------------------------------------------------------
def test_speak_reports_a_silent_backend_as_unable_to_speak(
    monkeypatch: pytest.MonkeyPatch, silent_session: VoiceSession
) -> None:
    monkeypatch.setattr(api, "get_voice", lambda: silent_session)

    body = asyncio.run(_route("/voice/speak")(api.VoiceTextBody(text="hello"), None))

    assert body["ok"] is True, "the backend did what it was asked: recorded the text"
    assert body["played"] is False
    assert body["backend"] == "null"
    assert body["can_speak"] is False
    assert "plays nothing" in body["reason"]


def test_speak_reports_a_backend_fault_as_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ExplodingTTS(NullTTS):
        name = "exploding"

        async def speak(self, text, destination=None) -> TTSResult:
            raise OSError("audio device is busy")

    session = VoiceSession(_Core(), tts=_ExplodingTTS(), stt=TranscriptSTT())
    monkeypatch.setattr(api, "get_voice", lambda: session)

    # A backend fault must be a reported failure, not an exception escaping the route.
    body = asyncio.run(_route("/voice/speak")(api.VoiceTextBody(text="hello"), None))

    assert body["ok"] is False
    assert "audio device is busy" in body["error"]


def test_command_reports_speech_failure_without_losing_the_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ExplodingTTS(NullTTS):
        name = "exploding"

        async def speak(self, text, destination=None) -> TTSResult:
            raise OSError("audio device is busy")

    session = VoiceSession(_Core(), tts=_ExplodingTTS(), stt=TranscriptSTT())
    monkeypatch.setattr(api, "get_voice", lambda: session)

    body = asyncio.run(
        _route("/voice/command")(
            api.VoiceTextBody(text="what time is it", speak=True, require_wake=False), None
        )
    )

    assert body["rejected"] is False
    assert body["reply"] == "it is noon", "the answer must survive a dead speaker"
    assert body["spoken"] is False
    assert "audio device is busy" in body["speech_error"]


def test_command_never_claims_silence_was_speech(
    monkeypatch: pytest.MonkeyPatch, silent_session: VoiceSession
) -> None:
    monkeypatch.setattr(api, "get_voice", lambda: silent_session)

    body = asyncio.run(
        _route("/voice/command")(
            api.VoiceTextBody(text="what time is it", speak=True, require_wake=False), None
        )
    )

    assert body["spoken"] is False
    assert body["speech_error"] == "", "nothing failed; the silence was a choice"


# ---------------------------------------------------------------------------
# /voice/status truthfully reports the configured backend
# ---------------------------------------------------------------------------
def test_status_reports_a_disabled_voice_as_unable_to_speak(silent_session: VoiceSession) -> None:
    silent_session.settings = Settings(tts_enabled=False)
    status = silent_session.status()

    assert status["tts"]["can_speak"] is False
    assert status["tts"]["enabled"] is False
    assert "JARVIS_TTS_ENABLED" in status["tts"]["reason"]