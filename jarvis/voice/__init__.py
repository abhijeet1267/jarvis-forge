"""Voice package (Phase 2).

    wake word -> listen -> transcribe -> JarvisCore.handle -> speak

Backends are pluggable and probed for availability, so the same session code
runs against macOS native speech, whisper.cpp, a cloud provider, or a text
passthrough for testing.
"""

from .audio import (
    AudioRecorder,
    AudioUnavailable,
    describe_audio,
    get_recorder,
    microphone_state,
    probe_mic,
    reset_install_cache,
    reset_mic_probe,
)
from .session import VoiceSession, VoiceTurn
from .stt import STTBackend, STTResult, describe_stt, get_stt
from .tts import TTSBackend, TTSResult, describe_tts, get_tts
from .wakeword import TextWakeWordDetector, WakeMatch, describe_wake

__all__ = [
    "AudioRecorder",
    "AudioUnavailable",
    "get_recorder",
    "describe_audio",
    "microphone_state",
    "probe_mic",
    "reset_install_cache",
    "reset_mic_probe",
    "STTBackend",
    "STTResult",
    "get_stt",
    "describe_stt",
    "TTSBackend",
    "TTSResult",
    "get_tts",
    "describe_tts",
    "TextWakeWordDetector",
    "WakeMatch",
    "describe_wake",
    "VoiceSession",
    "VoiceTurn",
]
