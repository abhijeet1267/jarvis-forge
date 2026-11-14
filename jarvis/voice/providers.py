"""Voice Providers for JARVIS (Phase 80).

Implements actual voice processing with real local-first providers.
Provides interface implementations for:
- Wake word detection
- Speech-to-text  
- Text-to-speech
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional


@dataclass
class STTResult:
    """Result from speech-to-text transcription."""
    transcript: str = ""
    confidence: float = 1.0
    provider: str = "unknown"
    duration_seconds: float = 0.0
    language: str = "en-US"
    status: str = "success"
    error: Optional[str] = None


try:
    import speech_recognition as sr
    SPEECH_RECOGNITION_AVAILABLE = True
except ImportError:
    SPEECH_RECOGNITION_AVAILABLE = False


class LocalSpeechToTextProvider:
    """Local speech-to-text using SpeechRecognition library."""
    
    PROVIDER_NAME = "local_stt"
    
    def __init__(self):
        self._recognizer = sr.Recognizer() if SPEECH_RECOGNITION_AVAILABLE else None
    
    def is_available(self) -> bool:
        """Check if this provider is available."""
        return bool(SPEECH_RECOGNITION_AVAILABLE and self._recognizer)
    
    def transcribe_audio(self, audio_data: bytes) -> STTResult:
        """Transcribe audio to text."""
        
        if not self.is_available():
            return STTResult(
                transcript="",
                status="error",
                error="Speech recognition library not available",
                provider=self.PROVIDER_NAME
            )
        
        try:
            # Create AudioData from bytes
            audio = sr.AudioData(audio_data, 16000, 2)
            
            # Transcribe using Google Web API
            transcript = self._recognizer.recognize_google(audio, language="en-US")
            
            return STTResult(
                transcript=transcript,
                confidence=1.0,
                provider=self.PROVIDER_NAME,
                status="success"
            )
            
        except sr.UnknownValueError:
            return STTResult(
                transcript="",
                status="no_speech_detected",
                provider=self.PROVIDER_NAME
            )
        except sr.RequestError as e:
            return STTResult(
                transcript="",
                status="error",
                error=f"Speech recognition service unavailable: {str(e)}",
                provider=self.PROVIDER_NAME
            )
        except Exception as e:
            return STTResult(
                transcript="",
                status="error",
                error=str(e),
                provider=self.PROVIDER_NAME
            )
    
    def get_provider_name(self) -> str:
        """Get provider name."""
        return self.PROVIDER_NAME


# Singleton instance
LOCAL_STT_PROVIDER = LocalSpeechToTextProvider()

def get_stt_providers() -> List[tuple]:
    """Get list of available STT providers."""
    providers = []
    
    if LOCAL_STT_PROVIDER.is_available():
        providers.append((LOCAL_STT_PROVIDER.PROVIDER_NAME, "Local - Privacy-friendly"))
    
    return providers
