"""Voice pipeline tests (Phase 2).

Covers wake-word matching, TTS synthesis, STT backend selection, session
orchestration, follow-up handling and barge-in.
"""

from __future__ import annotations

import asyncio
import platform
from pathlib import Path

import pytest

from jarvis.voice import (
    TextWakeWordDetector,
    VoiceSession,
    describe_audio,
    describe_stt,
    describe_tts,
    describe_wake,
    get_stt,
    get_tts,
)
from jarvis.voice.stt import STTResult, TranscriptSTT
from jarvis.voice.tts import MacOSSayTTS, NullTTS, TTSResult


# ---------------------------------------------------------------------------
# Wake word
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "phrase,expected_command",
    [
        ("Jarvis open VS Code", "open VS Code"),
        ("jarvis, what is my battery", "what is my battery"),
        ("Hey Jarvis find my report", "find my report"),
        ("OK Jarvis, take a screenshot", "take a screenshot"),
    ],
)
def test_wake_word_detected_and_stripped(phrase: str, expected_command: str) -> None:
    match = TextWakeWordDetector().detect(phrase)
    assert match.detected
    assert match.command == expected_command


def test_wake_word_preserves_paths_exactly() -> None:
    """Normalising the transcript must never mangle a file path."""
    match = TextWakeWordDetector().detect("Jarvis, delete /tmp/JarvisDemo/AMR_Report.pdf")
    assert match.detected
    assert match.command == "delete /tmp/JarvisDemo/AMR_Report.pdf"


@pytest.mark.parametrize("phrase", ["jervis open chrome", "jarvis! open chrome", "jarvis, open chrome"])
def test_wake_word_tolerates_recognition_noise(phrase: str) -> None:
    """Speech recognition routinely mishears 'Jarvis' -- do not be brittle."""
    match = TextWakeWordDetector().detect(phrase)
    assert match.detected
    assert "open chrome" in match.command


@pytest.mark.parametrize("phrase", ["open VS Code", "what is my battery", "travis open chrome"])
def test_no_false_wake_positives(phrase: str) -> None:
    assert not TextWakeWordDetector().detect(phrase).detected


def test_wake_word_alone_yields_no_command() -> None:
    match = TextWakeWordDetector().detect("Jarvis")
    assert match.detected
    assert not match.has_command


def test_describe_wake_reports_configuration() -> None:
    info = describe_wake()
    assert "jarvis" in info["wake_words"]
    assert info["mode"] == "transcript_matching"


# ---------------------------------------------------------------------------
# TTS
# ---------------------------------------------------------------------------
def test_tts_backend_is_selected() -> None:
    backend = get_tts()
    assert backend.available()
    assert backend.name in {"macos_say", "pyttsx3", "null"}


def test_describe_tts_shape() -> None:
    info = describe_tts()
    assert "available" in info
    assert "macos_say" in info["available"]


@pytest.mark.skipif(not MacOSSayTTS().available(), reason="macOS `say` not available")
def test_macos_tts_synthesises_real_audio() -> None:
    """The macOS backend must produce a genuine, non-empty audio file."""
    result = asyncio.run(MacOSSayTTS().synthesize("Jarvis Forge voice test."))

    assert result.ok, result.error
    assert result.audio_path is not None
    assert result.audio_path.exists()
    assert result.audio_path.stat().st_size > 1000
    assert result.duration_ms > 0


def test_null_tts_records_utterances() -> None:
    backend = NullTTS()
    asyncio.run(backend.speak("hello there"))
    asyncio.run(backend.speak("second line"))
    assert backend.spoken == ["hello there", "second line"]


def test_tts_empty_text_is_rejected() -> None:
    result = asyncio.run(NullTTS().synthesize("   "))
    assert not result.ok or result.meta.get("silent")


# ---------------------------------------------------------------------------
# STT
# ---------------------------------------------------------------------------
def test_stt_backend_is_selected() -> None:
    backend = get_stt()
    assert backend.available()
    assert backend.name in {"macos_speech", "whisper_cpp", "openai_whisper", "transcript"}


def test_transcript_stt_passthrough(tmp_area: Path) -> None:
    clip = tmp_area / "clip.wav"
    clip.write_bytes(b"not really audio")

    backend = TranscriptSTT(text="open vs code")
    result = asyncio.run(backend.transcribe(clip))

    assert result.ok
    assert result.text == "open vs code"


def test_describe_stt_shape() -> None:
    info = describe_stt()
    assert "available" in info
    assert info["available"]["transcript"] is True


def test_describe_audio_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_consent(monkeypatch, permission="authorized")
    info = describe_audio()
    assert "capture_available" in info
    assert isinstance(info["recorders"], list)


# ---------------------------------------------------------------------------
# Microphone consent is measured, not inferred from an installed binary
# ---------------------------------------------------------------------------
def _fake_consent(
    monkeypatch: pytest.MonkeyPatch,
    *,
    permission: str = "notDetermined",
    known: bool = True,
    pending: bool = False,
    devices: list | None = None,
    reason: str = "",
) -> None:
    """Pin what AVFoundation answers, so these tests never need a real mic."""
    from jarvis.voice import audio as audio_module

    monkeypatch.setattr(
        audio_module,
        "probe_mic",
        lambda settings=None, *, wait=0.0: {
            "known": known,
            "pending": pending,
            "permission": permission,
            "age_seconds": 0.0,
            "devices": devices if devices is not None
            else [{"name": "MacBook Air Microphone", "in_use": False}],
            "reason": reason,
        },
    )


def _no_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvis.voice import audio as audio_module

    monkeypatch.setattr(audio_module.shutil, "which", lambda _name: None)
    monkeypatch.setattr(audio_module.SoundDeviceRecorder, "available", lambda self: False)
    # Backend detection is memoised for the process; this test needs its own view.
    monkeypatch.setattr(audio_module, "_installed", {})


def test_installed_backend_alone_does_not_mean_microphone_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bug this pass fixed: ``which('ffmpeg')`` reported a live microphone.

    ffmpeg being present and macOS allowing capture are different facts. A
    machine with the binary and no consent cannot record -- it stalls on a
    prompt the process cannot show.
    """
    _fake_consent(monkeypatch, permission="notDetermined")
    info = describe_audio()

    assert info["capture_available"] is False
    assert info["capture_state"] == "blocked"
    assert info["microphone"]["permission"] == "notDetermined"


def test_granted_consent_and_a_backend_make_capture_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_consent(monkeypatch, permission="authorized")
    info = describe_audio()

    assert info["capture_available"] is True
    assert info["capture_state"] == "ready"
    assert info["microphone"]["devices"][0]["name"] == "MacBook Air Microphone"


def test_missing_backend_is_reported_separately_from_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """"allowed but nothing to record with" is not the same fault as "blocked"."""
    _fake_consent(monkeypatch, permission="authorized")
    _no_backend(monkeypatch)

    assert describe_audio()["capture_state"] == "no_backend"


def test_unmeasurable_consent_never_claims_a_fault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No probe (Linux, no swiftc) means "unknown", not "denied"."""
    _fake_consent(monkeypatch, permission="unknown", known=False, devices=[],
                  reason="Microphone consent is only readable on macOS.")
    info = describe_audio()

    assert info["capture_state"] == "unknown"
    assert info["capture_available"] is False
    # Not knowing is not a reason to refuse to try.
    from jarvis.voice.audio import FfmpegRecorder

    assert FfmpegRecorder().blocked() == ""


def test_measuring_is_not_reported_as_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An answer on its way is not a machine that cannot answer.

    Both used to render "consent unreadable", which told a user their
    microphone was unmeasurable while the probe was still working.
    """
    from jarvis.voice.audio import FfmpegRecorder, microphone_state

    _fake_consent(monkeypatch, known=False, pending=True, devices=[],
                  reason="measuring microphone consent for the first time")
    mic = microphone_state()
    info = describe_audio()

    assert mic["state"] == "measuring"
    assert info["capture_state"] == "measuring"
    assert info["capture_available"] is False
    # Being mid-measurement is still not evidence of a fault, so the recorder
    # may try rather than refuse.
    assert FfmpegRecorder().blocked() == ""


@pytest.mark.parametrize(
    "case",
    ["not_macos", "no_swiftc", "compile_fails", "helper_fails",
     "helper_garbage", "helper_times_out"],
)
def test_probe_degrades_to_unknown_without_guessing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str
) -> None:
    """Every way the probe can fail must say so, and never invent a permission."""
    import subprocess as real_subprocess

    from jarvis.voice import audio as audio_module
    from jarvis.voice.audio import MicProbe

    probe = MicProbe()
    monkeypatch.setattr(
        MicProbe, "binary_path", property(lambda self: tmp_path / "mic_state")
    )

    if case == "not_macos":
        monkeypatch.setattr(audio_module.platform, "system", lambda: "Linux")
    elif case == "no_swiftc":
        monkeypatch.setattr(audio_module.shutil, "which", lambda _n: None)
    elif case == "compile_fails":
        class Done:
            returncode = 1
            stdout = ""
            stderr = "error: no such module 'AVFoundation'"

        monkeypatch.setattr(audio_module.subprocess, "run", lambda *a, **k: Done())
    else:
        # Let the build "succeed" by writing the binary, then fail on the run.
        binary = tmp_path / "mic_state"

        class Run:
            def __call__(self, argv, **kwargs):
                if argv[0] == str(binary):
                    if case == "helper_fails":
                        class Bad:
                            returncode, stdout, stderr = 1, "", "crashed"

                        return Bad()
                    if case == "helper_garbage":
                        class Garbage:
                            returncode, stdout, stderr = 0, "not json at all", ""

                        return Garbage()
                    raise real_subprocess.TimeoutExpired(argv, timeout=1)

                binary.write_bytes(b"\x00fake")

                class Ok:
                    returncode, stdout, stderr = 0, "", ""

                return Ok()

        monkeypatch.setattr(audio_module.shutil, "which", lambda _n: "swiftc")
        monkeypatch.setattr(audio_module.subprocess, "run", Run())

    result = probe._measure()

    assert result["known"] is False, case
    assert result["permission"] == "unknown", case
    assert result["devices"] == [], case
    assert result["reason"], f"{case} must explain itself"


def test_build_failures_are_not_all_the_same_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """"No swiftc" and "not macOS" must not collapse into one reason."""
    from jarvis.voice import audio as audio_module
    from jarvis.voice.audio import MicProbe

    def reason(*, linux: bool = False, no_compiler: bool = False) -> str:
        probe = MicProbe()
        monkeypatch.setattr(
            MicProbe, "binary_path", property(lambda self: tmp_path / "mic_state")
        )
        # Pinned both ways: monkeypatch keeps earlier patches for the whole
        # test, so a second call that only removes one would still be "Linux".
        monkeypatch.setattr(
            audio_module.platform, "system", lambda: "Linux" if linux else "Darwin"
        )
        if no_compiler:
            monkeypatch.setattr(audio_module.shutil, "which", lambda _n: None)
        return probe._measure()["reason"]

    assert "macOS" in reason(linux=True)
    assert "swiftc" in reason(no_compiler=True)


def test_denied_consent_explains_how_to_fix_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jarvis.voice.audio import FfmpegRecorder

    _fake_consent(monkeypatch, permission="denied")
    recorder = FfmpegRecorder()

    assert recorder.available() is True  # the binary is still there
    assert "Privacy & Security" in recorder.blocked()


def test_recorder_refuses_before_the_capture_can_hang(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without consent ffmpeg blocks; the reason returned must be the consent."""
    import asyncio

    from jarvis.voice import audio as audio_module
    from jarvis.voice.audio import AudioUnavailable, FfmpegRecorder

    calls: list = []

    async def never_run(argv, timeout=None):  # noqa: ARG001
        calls.append(argv)
        return 0, "", ""

    monkeypatch.setattr(audio_module, "run_command", never_run)
    _fake_consent(monkeypatch, permission="notDetermined")

    with pytest.raises(AudioUnavailable) as exc:
        asyncio.run(FfmpegRecorder().record(1.0, tmp_path / "clip.wav"))

    assert calls == [], "ffmpeg must not be launched when consent is known-missing"
    assert "signed app bundle" in str(exc.value)


def test_consent_is_attributed_to_the_process_that_was_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TCC grants are per bundle, so a terminal's answer is not the app's."""
    import sys

    from jarvis.voice.audio import consent_host

    monkeypatch.setattr(
        sys, "executable", "/Applications/Jarvis Forge.app/Contents/MacOS/JarvisForge"
    )
    assert consent_host() == "/Applications/Jarvis Forge.app"

    monkeypatch.setattr(sys, "executable", "/usr/local/bin/python3")
    assert "unbundled process" in consent_host()


def test_launcher_says_why_the_microphone_is_unusable() -> None:
    """The desktop app's status lines must not reduce consent to "unavailable"."""
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "jarvis_launcher", Path(__file__).resolve().parents[1] / "app" / "launcher.py"
    )
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)

    ready = {"capture_available": True, "capture_state": "ready",
             "microphone": {"devices": [{"name": "Built-in"}]}}
    assert launcher.mic_note(ready) == "ready (1 input device(s))"

    denied = {"capture_available": False, "capture_state": "blocked",
              "microphone": {"detail": "macOS has denied this app the microphone."}}
    assert launcher.mic_note(denied).startswith("blocked by macOS permission — macOS has denied")

    # An older server that reports no consent detail still gets a readable line,
    # and never the literal string "None".
    assert launcher.mic_note({"capture_available": False}) == "permission state unreadable"
    assert launcher.mic_note({"capture_available": True}) == "ready"


def test_bundled_launcher_has_not_drifted_from_source() -> None:
    """The app bundle copies ``app/launcher.py`` instead of importing it.

    Nothing else noticed when the built bundle kept an older copy for a day,
    which is how it ended up shipping a microphone line the source no longer
    had. Cheap insurance against the same silence.
    """
    import filecmp

    root = Path(__file__).resolve().parents[1]
    bundled = root / "dist" / "Jarvis Forge.app" / "Contents" / "Resources" / "launcher.py"
    if not bundled.exists():
        pytest.skip("desktop app has not been built on this machine")

    assert filecmp.cmp(root / "app" / "launcher.py", bundled, shallow=False), (
        "dist/ ships a stale launcher.py — re-run build_app.py"
    )


@pytest.mark.skipif(platform.system() != "Darwin", reason="AVFoundation is macOS only")
def test_probe_reports_a_real_permission_state_on_this_machine() -> None:
    """End-to-end evidence: the helper builds and answers with real hardware.

    Deliberately unmocked -- it is the check that the mocked tests above
    describe a probe that actually exists and returns sane values.
    """
    from jarvis.voice.audio import probe_mic

    result = probe_mic()

    assert result["permission"] in {
        "authorized", "denied", "notDetermined", "restricted", "unknown"
    }
    if result["known"]:
        assert isinstance(result["devices"], list)


def test_first_run_of_a_freshly_built_helper_gets_the_longer_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """macOS assesses a never-run binary (~8s measured) before it answers.

    Using the steady-state 5s budget there reported "consent unreadable" on a
    machine that was actually fine.
    """
    from jarvis.voice import audio as audio_module
    from jarvis.voice.audio import MicProbe

    recorded: list = []

    class Done:
        returncode = 0
        stdout = '{"permission":"authorized","devices":[]}'
        stderr = ""

    def fake_run(argv, **kwargs):
        recorded.append(kwargs["timeout"])
        return Done()

    monkeypatch.setattr(audio_module.subprocess, "run", fake_run)
    probe = MicProbe()
    # A home where the helper has genuinely never been built, so this does not
    # depend on whether an earlier test already compiled it.
    absent = tmp_path / "mic_state"
    monkeypatch.setattr(MicProbe, "binary_path", property(lambda self: absent))
    monkeypatch.setattr(probe, "_build", lambda: absent)

    probe._measure()
    probe._measure()

    assert recorded == [audio_module._FIRST_RUN_TIMEOUT, audio_module._PROBE_TIMEOUT]


def test_status_never_waits_for_the_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """A health endpoint that blocks on a Swift build timed out the app's 3s client."""
    import threading
    import time

    from jarvis.voice.audio import MicProbe

    probe = MicProbe()
    started = threading.Event()
    release = threading.Event()

    def slow_measure() -> dict:
        started.set()
        release.wait(timeout=5)
        return {"known": True, "permission": "authorized", "devices": [], "reason": ""}

    monkeypatch.setattr(probe, "_measure", slow_measure)

    t = time.perf_counter()
    first = probe.read()
    elapsed = time.perf_counter() - t

    assert elapsed < 0.5, "read() must return at once when nothing is cached"
    assert first["permission"] == "unknown"
    assert first["known"] is False
    assert "first time" in first["reason"]

    assert started.wait(timeout=2), "the measurement must have been scheduled"
    release.set()

    # Once the background thread lands, the same call answers for real.
    for _ in range(200):
        if probe.read()["permission"] == "authorized":
            break
        time.sleep(0.01)
    else:
        pytest.fail("the cached measurement never became visible")

    settled = probe.read()
    assert settled["known"] is True
    assert isinstance(settled["age_seconds"], float)


def test_capture_affords_the_first_measurement(monkeypatch: pytest.MonkeyPatch) -> None:
    """Recording must not answer "unknown" and then hang in ffmpeg behind it."""
    import threading
    import time

    from jarvis.voice import audio as audio_module
    from jarvis.voice.audio import MicProbe

    probe = MicProbe()
    release = threading.Event()

    def slow_measure() -> dict:
        release.wait(timeout=5)
        return {"known": True, "permission": "denied", "devices": [], "reason": ""}

    monkeypatch.setattr(probe, "_measure", slow_measure)
    monkeypatch.setattr(audio_module, "_probe", probe)

    result: dict = {}

    def ask() -> None:
        result.update(audio_module.microphone_state(wait=2.0))

    worker = threading.Thread(target=ask)
    worker.start()
    time.sleep(0.2)
    assert not result, "wait= must hold the caller until the measurement lands"
    release.set()
    worker.join(timeout=5)

    assert result["state"] == "blocked"
    assert "Privacy & Security" in result["detail"]



def test_macos_speech_stops_being_selected_after_tcc_abort() -> None:
    """A TCC abort must be remembered, not retried on every request."""
    import shutil

    from jarvis.voice.stt import MacOSSpeechSTT

    backend = MacOSSpeechSTT()
    marker = backend._blocked_marker
    toolchain_present = shutil.which("swiftc") is not None

    had_marker = marker.exists()
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("blocked")

        # Blocked means "do not select me", regardless of the toolchain.
        assert backend.available() is False
    finally:
        if not had_marker:
            marker.unlink(missing_ok=True)

    # With the marker cleared, availability tracks the toolchain again.
    assert MacOSSpeechSTT().available() is toolchain_present


def test_describe_stt_includes_notes() -> None:
    info = describe_stt()
    assert "notes" in info
    assert isinstance(info["notes"], list)


# ---------------------------------------------------------------------------
# Session orchestration
# ---------------------------------------------------------------------------
@pytest.fixture()
def session(core) -> VoiceSession:
    """A voice session with deterministic TTS/STT so tests never play audio."""
    return VoiceSession(core, tts=NullTTS(), stt=TranscriptSTT(text=""))


def test_session_requires_wake_word(session: VoiceSession) -> None:
    turn = asyncio.run(session.process_text("what is my battery", speak=False))
    assert turn.rejected
    assert "wake word" in turn.reject_reason


def test_session_runs_command_after_wake_word(session: VoiceSession) -> None:
    turn = asyncio.run(session.process_text("Jarvis, what is my battery", speak=False))
    assert not turn.rejected
    assert turn.response is not None
    assert turn.response.actions[0].ok
    assert "battery" in turn.reply_text.lower()


def test_session_speaks_when_asked(session: VoiceSession) -> None:
    turn = asyncio.run(session.process_text("Jarvis, what time is it", speak=True))
    assert turn.speech is not None
    assert turn.speech.ok
    assert session.tts.spoken, "the reply should have been spoken"


def test_session_blocks_dangerous_commands(core) -> None:
    session = VoiceSession(core, tts=NullTTS(), stt=TranscriptSTT())
    turn = asyncio.run(session.process_text("Jarvis, delete everything", speak=False))
    assert not turn.rejected
    assert turn.response.decision.value == "BLOCK"


def test_followup_does_not_need_wake_word(session: VoiceSession) -> None:
    """After JARVIS asks something, a bare answer must be understood."""
    asyncio.run(session.process_text("Jarvis, delete this", speak=False))
    assert session.awaiting_followup

    turn = asyncio.run(session.process_text("yes", speak=False))
    assert not turn.rejected


def test_yes_resolves_pending_confirmation(core, tmp_area: Path) -> None:
    session = VoiceSession(core, tts=NullTTS(), stt=TranscriptSTT())
    target = tmp_area / "voice_delete.txt"
    target.write_text("x")

    first = asyncio.run(session.process_text(f"Jarvis, delete {target}", speak=False))
    assert first.response.needs_confirmation
    assert target.exists()

    second = asyncio.run(session.process_text("yes", speak=False))
    assert not second.rejected
    assert not target.exists(), "confirming with 'yes' should have run the delete"
    assert session.pending_confirmation is None


def test_no_cancels_pending_confirmation(core, tmp_area: Path) -> None:
    session = VoiceSession(core, tts=NullTTS(), stt=TranscriptSTT())
    target = tmp_area / "voice_keep.txt"
    target.write_text("x")

    asyncio.run(session.process_text(f"Jarvis, delete {target}", speak=False))
    second = asyncio.run(session.process_text("no", speak=False))

    assert target.exists(), "denying must not delete anything"
    assert "cancel" in second.reply_text.lower()


def test_interruption_stops_speaking(session: VoiceSession) -> None:
    asyncio.run(session.stop_speaking())
    assert session.tts._interrupted is True


def test_status_reports_all_subsystems(session: VoiceSession) -> None:
    status = session.status()
    assert {"enabled", "wake", "tts", "stt", "audio", "conversation"} <= set(status)
    assert "ready_for_microphone" in status


def test_reset_clears_conversation(session: VoiceSession) -> None:
    asyncio.run(session.process_text("Jarvis, what is my battery", speak=False))
    assert session.session_id

    session.reset()
    assert session.session_id is None
    assert session.pending_confirmation is None
    assert not session.awaiting_followup


def test_process_audio_uses_stt_then_pipeline(core) -> None:
    stt = TranscriptSTT(text="Jarvis, what is my battery")
    session = VoiceSession(core, tts=NullTTS(), stt=stt)

    clip = Path("/tmp/jarvis_voice_clip.wav")
    clip.write_bytes(b"fake")

    turn = asyncio.run(session.process_audio(clip, speak=False))
    assert turn.stt is not None and turn.stt.ok
    assert turn.transcript == "Jarvis, what is my battery"
    assert turn.response.actions[0].ok


def test_stt_failure_is_reported_not_swallowed(core) -> None:
    class BrokenSTT(TranscriptSTT):
        name = "broken"

        async def transcribe(self, audio_path: Path) -> STTResult:
            return STTResult(False, self.name, error="microphone permission denied")

    session = VoiceSession(core, tts=NullTTS(), stt=BrokenSTT())
    turn = asyncio.run(session.process_audio(Path("/tmp/none.wav"), speak=False))

    assert turn.rejected
    assert "permission" in turn.reject_reason


def test_empty_input_is_rejected(session: VoiceSession) -> None:
    turn = asyncio.run(session.process_text("   ", speak=False))
    assert turn.rejected
