"""Phase 43: a passthrough is not a microphone.

The ask was voice from the phone. Before building anything on top of the speech path, one
real audio clip was made with `say` and sent to the running app:

    POST /voice/transcribe  file=p43.aiff
    -> {"ok": true, "backend": "transcript", "text": "", "confidence": 1.0,
        "error": "", "duration_ms": 0}

Nothing on this machine can transcribe -- macOS Speech is TCC-blocked, whisper.cpp is not
installed, hosted Whisper has no key -- so `get_stt()` fell through to
:class:`TranscriptSTT`, a backend whose whole job is to echo text a *test* fed it. Handed a
real recording it answered **ok, confidence 1.0, no text**. A mic button wired to that would
report a successful hearing of nothing, and the same shape is what the same file already
caught four times in other parts of this build: a stand-in answering a production request.

The fix is at the source of the lie, not at the caller: the passthrough does not transcribe
audio, and an endpoint that has no microphone says so and names what would give it one.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from jarvis.config import Settings
from jarvis.voice import stt as stt_module


@pytest.fixture()
def clip(tmp_path):
    """A real audio file, not a fixture standing in for one."""
    made = tmp_path / "spoken.aiff"
    made.write_bytes(b"AIFF dummy payload, 2 seconds of nothing")
    return made


@pytest.fixture()
def no_microphone(monkeypatch):
    """A machine where every real backend is unavailable, as measured on this one."""
    monkeypatch.setattr(stt_module.MacOSSpeechSTT, "available", lambda self: False)
    monkeypatch.setattr(stt_module.WhisperCppSTT, "available", lambda self: False)
    monkeypatch.setattr(stt_module.OpenAIWhisperSTT, "available", lambda self: False)
    return Settings


# ---------------------------------------------------------------------------
# the passthrough
# ---------------------------------------------------------------------------


def test_the_passthrough_does_not_claim_to_have_heard_a_file(clip):
    """`TranscriptSTT` exists so tests can feed a transcript; nobody fed it anything."""
    backend = stt_module.TranscriptSTT()
    result = asyncio.run(backend.transcribe(clip))
    assert result.ok is False, (
        f"a passthrough reported ok=True for {backend.name}: {result}")
    assert result.confidence < 1.0 or result.text == ""


def test_a_fed_transcript_is_still_the_test_harness_it_was(clip):
    """The fix must not break the thing this class is for."""
    backend = stt_module.TranscriptSTT(text="what is my battery")
    result = asyncio.run(backend.transcribe(clip))
    assert result.ok is True and result.text == "what is my battery"
    assert result.meta.get("passthrough") is True, (
        "a transcript that came from a test harness must say so")


def test_a_backend_that_cannot_listen_is_marked_as_one(no_microphone, clip):
    """The selection needs to say *which kind* of answer it is handing back."""
    backend = stt_module.get_stt(no_microphone())
    assert backend.real is False, (
        f"{backend.name} was selected as if it could transcribe")
    result = asyncio.run(backend.transcribe(clip))
    assert result.ok is False


# ---------------------------------------------------------------------------
# the endpoint the phone would call
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(core):
    import importlib

    from fastapi.testclient import TestClient

    from jarvis.config import get_settings

    # `jarvis.api.app` is shadowed by the FastAPI instance the package re-exports,
    # so the module has to be reached through the import machinery.
    app_module = importlib.import_module("jarvis.api.app")
    headers = {"X-JARVIS-Token": get_settings().api_token}
    with TestClient(app_module.app, base_url="http://127.0.0.1:8765",
                    headers=headers) as test_client:
        yield test_client


def test_uploading_a_clip_with_no_microphone_says_so(client, clip, monkeypatch, no_microphone):
    """/voice/transcribe must never answer ok for a machine that cannot listen."""
    import importlib

    stt = importlib.import_module("jarvis.voice.stt")
    monkeypatch.setattr(stt.MacOSSpeechSTT, "available", lambda self: False)
    monkeypatch.setattr(stt.WhisperCppSTT, "available", lambda self: False)
    monkeypatch.setattr(stt.OpenAIWhisperSTT, "available", lambda self: False)
    body = client.post("/voice/transcribe",
                       files={"file": (clip.name, clip.read_bytes(), "audio/aiff")}).json()
    assert body.get("ok") is not True, (
        f"a machine with no microphone reported a successful transcription: {body}")
    assert not (body.get("ok") is True and body.get("backend") in ("transcript", None, "")), body


def test_the_deaf_answer_names_what_would_give_it_ears(client, clip, monkeypatch):
    """An honest failure is also a usable one: it says what would make it work."""
    import importlib

    stt = importlib.import_module("jarvis.voice.stt")
    monkeypatch.setattr(stt.MacOSSpeechSTT, "available", lambda self: False)
    monkeypatch.setattr(stt.WhisperCppSTT, "available", lambda self: False)
    monkeypatch.setattr(stt.OpenAIWhisperSTT, "available", lambda self: False)

    body = client.post("/voice/transcribe",
                       files={"file": (clip.name, clip.read_bytes(), "audio/aiff")}).json()
    assert body["ok"] is False, body
    text = str(body).lower()
    assert any(word in text for word in
               ("speech recognition", "whisper", "microphone")), (
        "the answer should name what would give it ears: " + str(body))


def test_voice_status_says_there_is_no_microphone_today(client):
    stt = client.get("/voice/status").json().get("stt") or {}
    available = stt.get("available") or {}
    real = any(available.get(k) for k in ("macos_speech", "whisper_cpp", "openai_whisper"))
    if not real:
        assert stt.get("can_listen") is False, (
            f"the page must be able to tell a microphone from none: {stt}")
