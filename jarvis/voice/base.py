"""Voice Runtime Base Classes (Phase 80).

Provider-neutral interfaces for voice processing:
- WakeWordProvider: Detect wake word ("Jarvis")
- SpeechToTextProvider: Transcribe audio to text
- TextToSpeechProvider: Convert text to spoken response
- PermissionDetector: Check microphone/audio permissions

Architecture must separate voice layers from business logic:
MIC -> WAKE WORD -> STT -> INTENT -> PLAN -> AUTHORITY -> EXECUTOR -> VERIFICATION -> TTS
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional


class PermissionState(str, enum.Enum):
    """Microphone/audio permission states."""
    GRANTED = "granted"
    DENIED = "denied"
    NOT_DETERMINED = "not_determined"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class VoiceRuntimeState(str, enum.Enum):
    """Voice runtime operational states."""
    IDLE = "idle"
    INITIALIZING = "initializing"
    LISTENING = "listening"
    WAKE_DETECTED = "wake_detected"
    PROCESSING = "processing"
    EXECUTING = "executing"
    RESPONDING = "responding"
    ERROR = "error"
    PERMISSION_REQUIRED = "permission_required"
    MUTED = "muted"


class PrivacyMode(str, enum.Enum):
    """Voice privacy modes."""
    OFF = "off"  # No voice listening
    PUSH_TO_TALK = "push_to_talk"  # Manual activation
    WAKE_WORD = "wake_word"  # Wake word activation
    WAKE_WORD_WITH_CONFIRMATION = "wake_word_confirmation"  # Wake + approval for high-risk


@dataclass
class VoiceContext:
    """Per-session voice context."""
    session_id: str
    conversation_history: List[Dict[str, Any]] = field(default_factory=list)
    current_task_id: Optional[str] = None
    timeout_seconds: int = 30
    privacy_mode: PrivacyMode = PrivacyMode.OFF
    created_at: datetime = field(default_factory=datetime.utcnow)
    
    def add_message(self, role: str, content: str) -> None:
        """Add message to conversation history."""
        self.conversation_history.append({
            "role": role,
            "content": content,
            "timestamp": datetime.utcnow().isoformat(),
        })
        
        # Limit history to last 10 messages
        if len(self.conversation_history) > 10:
            self.conversation_history = self.conversation_history[-10:]


class WakeWordProvider(ABC):
    """Interface for wake word detection."""
    
    @abstractmethod
    def is_available(self) -> bool:
        """Check if this provider is available."""
        pass
    
    @abstractmethod
    def start_listening(self, callback: Callable[[str], None]) -> bool:
        """Start listening for wake word. Callback invoked when detected."""
        pass
    
    @abstractmethod
    def stop_listening(self) -> bool:
        """Stop listening for wake word."""
        pass
    
    @abstractmethod
    def get_state(self) -> VoiceRuntimeState:
        """Get current provider state."""
        pass


class SpeechToTextProvider(ABC):
    """Interface for speech-to-text conversion."""
    
    @abstractmethod
    def is_available(self) -> bool:
        """Check if this provider is available."""
        pass
    
    @abstractmethod
    def transcribe_audio(self, audio_data: bytes) -> str:
        """Transcribe audio to text."""
        pass
    
    @abstractmethod
    def transcribe_stream(self, stream) -> str:
        """Stream transcription."""
        pass
    
    @abstractmethod
    def get_provider_name(self) -> str:
        """Get provider name (local/openai/gemini/etc.)."""
        pass


class TextToSpeechProvider(ABC):
    """Interface for text-to-speech conversion."""
    
    @abstractmethod
    def is_available(self) -> bool:
        """Check if this provider is available."""
        pass
    
    @abstractmethod
    def speak(self, text: str) -> bool:
        """Convert text to speech."""
        pass
    
    @abstractmethod
    def cancel_speaking(self) -> bool:
        """Cancel current speech output."""
        pass
    
    @abstractmethod
    def speak_async(self, text: str, on_complete: Callable[[], None]) -> None:
        """Speak text asynchronously."""
        pass
    
    @abstractmethod
    def get_provider_name(self) -> str:
        """Get provider name."""
        pass


class PermissionDetector(ABC):
    """Interface for detecting microphone/audio permissions."""
    
    @abstractmethod
    def detect_microphone_permission(self) -> PermissionState:
        """Detect macOS microphone permission state."""
        pass
    
    @abstractmethod
    def detect_accessibility_permission(self) -> PermissionState:
        """Detect macOS Accessibility permission state."""
        pass
    
    @abstractmethod
    def get_permissions_summary(self) -> Dict[str, PermissionState]:
        """Get summary of all relevant permissions."""
        pass


class VoiceRuntimeController(ABC):
    """Base interface for controlling voice runtime."""
    
    @abstractmethod
    def initialize(self) -> bool:
        """Initialize voice runtime."""
        pass
    
    @abstractmethod
    def start(self, mode: PrivacyMode = PrivacyMode.WAKE_WORD) -> bool:
        """Start voice runtime in given mode."""
        pass
    
    @abstractmethod
    def stop(self) -> bool:
        """Stop voice runtime."""
        pass
    
    @abstractmethod
    def mute(self) -> bool:
        """Mute microphone input."""
        pass
    
    @abstractmethod
    def unmute(self) -> bool:
        """Unmute microphone input."""
        pass
    
    @abstractmethod
    def handle_interruption(self) -> bool:
        """Handle user interruption while responding."""
        pass
    
    @abstractmethod
    def get_state(self) -> VoiceRuntimeState:
        """Get current runtime state."""
        pass
    
    @abstractmethod
    def get_runtime_status(self) -> Dict[str, Any]:
        """Get comprehensive runtime status."""
        pass
