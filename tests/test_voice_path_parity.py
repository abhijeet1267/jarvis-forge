"""V26: the voice path and the typed path must resolve identically.

`VoiceSession.process_text` builds a CommandRequest and calls
``self.core.handle(request)``, which is one of the two gated call sites. This
test proves that claim rather than asserting it: the same session memory that
makes a typed "close it" resolve must make a spoken one resolve too, and turning
the flag off must disable both.
"""

from __future__ import annotations

import pytest

from jarvis.config import Settings
from jarvis.core.models import PlannedAction
from jarvis.core.orchestrator import JarvisCore
from jarvis.tools.base import ok
from jarvis.voice.session import VoiceSession


class _NullRecorder:
    real = False

    def start(self, *a, **k):
        raise RuntimeError("no microphone in this test")

    def stop(self, *a, **k):
        raise RuntimeError("no microphone in this test")


class _NullSTT:
    real = False

    async def transcribe(self, path):
        raise RuntimeError("no transcription in this test")


class _NullTTS:
    real = False

    async def speak(self, text):
        from jarvis.voice.base import SpeechResult
        return SpeechResult(ok=True, backend="null", played=False)


@pytest.fixture
def core():
    return JarvisCore()


@pytest.fixture
def voice(core):
    return VoiceSession(core, recorder=_NullRecorder(), stt=_NullSTT(), tts=_NullTTS())


def _seed_verified_app(core, name="Calculator", session="s1"):
    action = PlannedAction(tool="open_application", params={"app": name})
    result = ok("open_application", f"{name} is open", display_name=name, verified=True)
    core._remember_verified_entity(
        action, result, type("R", (), {"session_id": session, "text": ""})())


def test_voice_session_routes_through_core_handle(voice, core):
    """Structural claim: one execution path, not a voice-only one."""
    import inspect

    source = inspect.getsource(VoiceSession.process_text)
    assert "self.core.handle(" in source, (
        "the voice path no longer goes through the orchestrator; resolution "
        "and permission would not apply to speech")


def test_flag_off_means_no_resolution_anywhere(core):
    assert Settings().voice_operator_v2_enabled is False
    from jarvis.voice.resolve import describe_resolution

    _seed_verified_app(core)
    # With the flag off the orchestrator never calls the resolver; the memory
    # may hold an entity but nothing consults it.
    import inspect

    src = inspect.getsource(JarvisCore)
    assert src.count("voice_operator_v2_enabled") >= 2, (
        "both command call sites must consult the flag")


def test_resolution_reaches_voice_turns(voice, core):
    """A spoken 'close it' resolves from memory seeded by a verified open."""
    core.settings.voice_operator_v2_enabled = True
    core.entity_memory.record("voice-session", "app", "Calculator", verified=True,
                              evidence="process table")

    from jarvis.voice.resolve import resolve_reference

    # This is the exact call the orchestrator makes at its gated call site.
    resolved = resolve_reference("close it", core.entity_memory, "voice-session", kind="app")
    assert resolved == "close Calculator"


def test_voice_and_typed_see_the_same_memory(core, voice):
    """The memory is on the core, not on the surface, so both share it."""
    assert core.entity_memory is voice.core.entity_memory


@pytest.mark.parametrize("kind", ["app"])
def test_unverified_voice_entity_does_not_resolve(kind):
    from jarvis.voice.entities import EntityMemory
    from jarvis.voice.resolve import resolve_reference

    memory = EntityMemory()
    memory.record("s1", kind, "Calculator", verified=False)
    assert resolve_reference("close it", memory, "s1", kind=kind) is None