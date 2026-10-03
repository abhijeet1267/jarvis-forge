"""Phase 47: the mic was never broken by Apple — we broke it twice.

Measured on 2026-10-03, against the app the user launched with `open`, with a real
speech clip (`say -o /tmp/p43.aiff "what is my battery"`, 22 kHz mono AIFF):

1. From a shell-launched bundle the helper died with SIGABRT and the crash report said
   `namespace: TCC ... privacy-sensitive data without a usage description`. The app's own
   advice -- "Run JARVIS from a bundled host" -- was therefore telling the user to do the
   thing that was already true.
2. Launched properly, the prompt appeared, the user clicked Allow, and the call still
   failed: first "speech authorisation request timed out" (20 s), then "transcription
   timed out" (60 s). No Apple refusal anywhere in that chain: **the helper blocked the
   main thread on a semaphore while waiting for callbacks that the main queue delivers**,
   so the answer could never arrive on the thread that was waiting for it.
3. The helper the app built was `x86_64` on an `arm64` machine and took 4m08s to compile.
   The same source compiled natively in 2.2s. With the run-loop fix and an arm64 binary,
   the clip transcribed in **637 ms at 0.94 confidence**.

The tests below pin each of those, plus the vocabulary defect they exposed: `/voice/status`
answered `can_listen: true` *in the body of the response that had just failed*, and
`available()` has always meant "swiftc exists and nobody has refused us in this home yet"
while the abstract contract says "can transcribe on this machine" (43-B, 46-A).
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from jarvis.config import get_settings
from jarvis.voice import stt as stt_module
from jarvis.voice.stt import MacOSSpeechSTT, STTResult

REPO = Path(__file__).resolve().parents[1]
SWIFT = REPO / "jarvis" / "voice" / "native" / "macos_speech.swift"


@pytest.fixture()
def native():
    """A macOS-speech backend in the session's isolated home, with swiftc "present".

    Both markers are restored afterwards: a `.tcc_blocked` file left behind here is read
    by the next test that asks whether the toolchain is present, which is how one phase's
    green run turns into another phase's unexplained failure.
    """
    backend = MacOSSpeechSTT(get_settings())
    paths = (backend._blocked_marker, backend._proven_marker)
    saved = {path: path.read_bytes() if path.exists() else None for path in paths}
    for path in paths:
        path.unlink(missing_ok=True)
    try:
        yield backend
    finally:
        for path, blob in saved.items():
            if blob is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(blob)


# ---------------------------------------------------------------------------
# 1. the helper may not block the thread its callbacks arrive on
# ---------------------------------------------------------------------------


def test_the_helper_waits_by_pumping_the_run_loop():
    """A semaphore wait on the main thread waits for something only the main thread can
    deliver. This is the whole bug, and it is invisible in the error text: both failures
    read as timeouts, which is exactly what an Apple refusal looks like from the outside.
    """
    source = SWIFT.read_text()
    assert "RunLoop.current.run" in source, (
        "the helper blocks the main thread; Speech callbacks are delivered on it")
    assert "DispatchSemaphore" not in source, (
        "a semaphore wait is how this file deadlocked itself")


def test_both_speech_callbacks_return_to_the_main_queue():
    source = SWIFT.read_text()
    # requestAuthorization documents an arbitrary queue; recognitionTask reports on the
    # main queue. Both mutate state the waiting loop reads, so both must hop.
    assert source.count("DispatchQueue.main.async") >= 2, (
        "authorisation and recognition must both hand their answer back to the run loop")


def test_the_recognition_task_is_kept_alive():
    """An `SFSpeechRecognitionTask` with no strong reference can be released mid-flight,
    which would look identical to the timeout this phase just spent finding."""
    source = SWIFT.read_text()
    assert re.search(r"var keepAlive.*SFSpeechRecognitionTask|retain", source, re.S), (
        "the task is not retained, so it may be cancelled before it answers")


# ---------------------------------------------------------------------------
# 2. the helper is built for the machine it will run on
# ---------------------------------------------------------------------------


def test_the_build_pins_the_native_architecture(monkeypatch, tmp_path):
    """Measured: the app's helper was x86_64 on an arm64 Mac (4m08s to build); the same
    source built natively in 2.2s. Whatever set that target, it was inherited rather than
    chosen, and an inherited target is how a delivered app ends up running its own speech
    framework under translation.
    """
    import platform
    import subprocess

    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = list(argv)

        class Done:
            returncode = 0
            stdout = b""
            stderr = b""

        return Done()

    monkeypatch.setattr(subprocess, "run", fake_run)
    backend = MacOSSpeechSTT(get_settings())
    backend._ensure_binary()

    argv = captured.get("argv", [])
    joined = " ".join(argv)
    machine = platform.machine().lower()
    assert f"-target {machine}" in joined or f"-target={machine}" in joined, (
        f"swiftc is not pinned to this machine's architecture ({machine}): {joined[:160]}")


# ---------------------------------------------------------------------------
# 3. availability is a fact with three states, not a hope
# ---------------------------------------------------------------------------


def test_availability_reports_three_states(native):
    """43-B / 46-A: `available()` meant "swiftc exists and nobody has refused us in this
    home yet", and a fresh home therefore promised ears it had never tested.
    """
    assert native.state == "untested", native.state
    assert native.can_listen is False, "an untested backend may not claim it can hear"
    assert native.may_try is True, "untested must still allow the attempt that proves it"

    native._proven_marker.parent.mkdir(parents=True, exist_ok=True)
    native._proven_marker.write_text("ok\n")
    assert native.state == "proven"
    assert native.can_listen is True

    native._proven_marker.unlink()
    native._blocked_marker.write_text("refused\n")
    assert native.state == "refused"
    assert native.can_listen is False
    assert native.may_try is False, "a refused backend must not be offered again"


def test_a_successful_transcript_is_what_makes_a_backend_proven(native, monkeypatch):
    """The proof is a transcript, not a claim. Probing at boot costs a SIGABRT, so the
    state has to be earned by the first real attempt and then remembered.
    """
    class Proc:
        returncode = 0

        async def communicate(self):
            return (json.dumps({"ok": True, "text": "what is my battery",
                                "confidence": 0.94}).encode(), b"")

    async def fake_exec(*args, **kwargs):
        return Proc()

    monkeypatch.setattr(native, "_ensure_binary", lambda: Path("/bin/true"))
    monkeypatch.setattr(stt_module.asyncio, "create_subprocess_exec", fake_exec)

    result = asyncio.run(native.transcribe(Path("/tmp/p43.aiff")))
    assert result.ok and result.text == "what is my battery", result
    assert native._proven_marker.exists(), "a transcript that worked did not make the backend proven"
    assert native.state == "proven"


def test_a_refusal_is_remembered_as_refused_not_absent(native, monkeypatch):
    """SIGABRT is a specific answer: Apple refused this process. It must not be stored as
    "there is no such backend", because that loses the reason and the remedy."""
    class Proc:
        returncode = 134

        async def communicate(self):
            return b"", b""

    async def fake_exec(*args, **kwargs):
        return Proc()

    monkeypatch.setattr(native, "_ensure_binary", lambda: Path("/bin/true"))
    monkeypatch.setattr(stt_module.asyncio, "create_subprocess_exec", fake_exec)

    result = asyncio.run(native.transcribe(Path("/tmp/p43.aiff")))
    assert not result.ok
    assert native._blocked_marker.exists()
    assert native.state == "refused"


# ---------------------------------------------------------------------------
# 4. the advice has to name the cause that is actually left
# ---------------------------------------------------------------------------


def test_the_refusal_advice_does_not_send_the_user_to_a_done_task():
    """"Run JARVIS from a bundled host" was written when the only observed case was a bare
    binary. Measured today: from a bundled host, with the prompt granted, that advice is
    unactionable -- the thing it tells you to do is already true.
    """
    advice = stt_module.ears_advice()
    assert "bundled host" not in advice.lower() or "already" in advice.lower(), (
        f"the advice still tells the user to do something the app already does: {advice}")


# ---------------------------------------------------------------------------
# 5. the client must offer the mic it has not yet proven
# ---------------------------------------------------------------------------


def test_the_phone_offers_the_mic_on_an_untested_laptop():
    """`can_listen` now means *proven here*, which no fresh install is. A button gated on
    it alone could never be pressed for the first time, and the first attempt is the only
    thing that produces the proof -- so the client reads `may_try` and says which state
    the laptop is in.
    """
    source = (REPO / "dashboard" / "phone.html").read_text()
    assert "stt.may_try" in source, "the page still gates on the proof, not on the offer"
    assert "earState" in source, "the page does not distinguish untested from refused"
    assert "never tried on this laptop" in source, (
        "an untested laptop reads as a broken one, which sends the user to System Settings"
        " for a problem that does not exist yet")


def test_the_dashboard_says_which_state_the_ears_are_in():
    """43-C: the Voice card used to print the first truthy `available` entry, and
    `transcript` is always truthy -- so a machine with no microphone at all reported its
    own stand-in as the speech backend.
    """
    source = (REPO / "dashboard" / "index.html").read_text()
    assert "stt.state" in source, "the card still reads only `available`"
    assert 'line("STT", sttWord' in source, "the state is not shown where the backend is"


# ---------------------------------------------------------------------------
# 6. the endpoint may not claim ears in the body of a failure
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(core):
    import importlib

    from fastapi.testclient import TestClient

    app_module = importlib.import_module("jarvis/api/app".replace("/", "."))
    with TestClient(app_module.app, base_url="http://127.0.0.1:8765",
                    headers={"X-JARVIS-Token": get_settings().api_token}) as test_client:
        yield test_client


def test_a_failed_transcription_never_answers_can_listen_true(client, monkeypatch):
    """Measured live, twice:

        {"ok": false, ..., "error": "transcription timed out", "can_listen": true}

    A client that reads that field will keep offering a microphone the laptop just lost.
    """
    async def failing_transcribe(path):
        return STTResult(False, "macos_speech", error="transcription timed out")

    importlib = __import__("importlib")
    app_module = importlib.import_module("jarvis.api.app")
    session = app_module.get_voice()
    monkeypatch.setattr(session.stt, "transcribe", failing_transcribe)

    response = client.post("/voice/transcribe", files={"file": ("clip.aiff", b"x", "audio/aiff")})
    data = response.json()
    assert response.status_code == 200, data
    assert data["ok"] is False, data
    assert data["can_listen"] is False, (
        f"a refused transcription answered can_listen true: {data}")


def test_voice_status_exposes_the_state_not_only_a_boolean(client, monkeypatch):
    """`can_listen` alone cannot say "never tried", which is the state every fresh install
    is in and the one that most needs a different sentence on screen.
    """
    import importlib

    app_module = importlib.import_module("jarvis.api.app")
    stt = client.get("/voice/status").json()["stt"]
    assert "state" in stt, sorted(stt)
    assert stt["state"] in ("untested", "refused", "proven"), stt["state"]
    assert "may_try" in stt, sorted(stt)
    if stt["state"] != "proven":
        assert stt["can_listen"] is False, stt
