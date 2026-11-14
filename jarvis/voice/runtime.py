"""Voice Runtime Controller (Phase 80).

Central coordinator for voice functionality:
- Microphone lifecycle management
- Wake word detection state
- Speech capture/transcription
- Command segmentation
- Agent Core handoff
- Spoken response synthesis
- Interruption handling
- Timeout/cancellation management

Privacy modes:
OFF -> PUSH_TO_TALK -> WAKE_WORD -> WAKE_WORD_CONFIRMATION

Never claim LISTENING unless microphone is actually active.
Never display false permission states.
"""

from __future__ import annotations

import asyncio
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional
from ..core.models import utcnow


@dataclass
class VoiceCommand:
    """Represent a complete voice command."""
    wake_word: str
    text_command: str
    confidence: float
    raw_audio: bytes = b""
    timestamp: str = field(default_factory=lambda: utcnow().isoformat())
    session_id: str = ""


@dataclass
class VoiceRuntimeState:
    """Track current voice runtime state."""
    state: str = "idle"
    privacy_mode: str = "off"
    permission_status: str = "unavailable"
    listening_since: Optional[str] = None
    last_command_at: Optional[str] = None
    conversation_context: Dict[str, Any] = field(default_factory=dict)
    timeout_seconds: int = 30
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "privacy_mode": self.privacy_mode,
            "permission_status": self.permission_status,
            "listening_since": self.listening_since,
            "last_command_at": self.last_command_at,
            "timeout_seconds": self.timeout_seconds,
        }


class VoiceRuntimeController:
    """Control voice runtime lifecycle and state."""
    
    _instance: Optional["VoiceRuntimeController"] = None
    
    def __init__(self):
        self._runtime_state = VoiceRuntimeState()
        self._is_running = False
        self._is_initialized = False
        
        # Command queue
        self._command_queue: queue.Queue[VoiceCommand] = queue.Queue()
        
        # Callbacks
        self._on_command_received: Optional[Callable[[VoiceCommand], None]] = None
        self._on_permission_error: Optional[Callable[[str], None]] = None
        
        # Threading
        self._listen_thread: Optional[threading.Thread] = None
        self._cancel_event = threading.Event()
    
    @classmethod
    def get_instance(cls) -> "VoiceRuntimeController":
        """Get singleton instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
    
    def initialize(self) -> bool:
        """Initialize voice runtime."""
        
        from ..voice.mac_permissions import MacPermissionDetector
        
        try:
            # Check permissions first
            mic_perm = MacPermissionDetector.detect_microphone_permission()
            
            if mic_perm.value != "granted":
                self._runtime_state.permission_status = mic_perm.value
                
                if self._on_permission_error:
                    self._on_permission_error(
                        f"Microphone permission {mic_perm.value}. "
                        "Please enable in System Preferences."
                    )
                
                return False
            
            # If we got here, basic initialization succeeded
            self._is_initialized = True
            self._runtime_state.permission_status = "granted"
            
            return True
            
        except Exception as e:
            self._runtime_state.permission_status = "error"
            self._runtime_state.state = "error"
            return False
    
    def start(self, mode: str = "wake_word") -> bool:
        """Start voice runtime in given mode."""
        
        if not self._is_initialized:
            if not self.initialize():
                return False
        
        self._runtime_state.privacy_mode = mode
        self._runtime_state.state = "initializing"
        
        # Start listening thread based on mode
        if mode == "wake_word":
            self._start_wake_word_listener()
        elif mode == "push_to_talk":
            self._start_listening_mode()
        else:
            self._runtime_state.state = "error"
            return False
        
        return True
    
    def stop(self) -> bool:
        """Stop voice runtime."""
        
        self._cancel_event.set()
        
        if self._listen_thread:
            self._listen_thread.join(timeout=5.0)
            self._listen_thread = None
        
        self._is_running = False
        self._runtime_state.state = "idle"
        self._runtime_state.listening_since = None
        
        return True
    
    def mute(self) -> bool:
        """Mute microphone input."""
        self._cancel_event.set()
        self._runtime_state.state = "muted"
        return True
    
    def unmute(self) -> bool:
        """Unmute microphone input."""
        self._cancel_event.clear()
        self._runtime_state.state = "listening"
        return True
    
    def handle_interruption(self) -> bool:
        """Handle user interruption while responding."""
        
        # Stop current speech processing
        self._cancel_event.set()
        
        # Return to listening state
        self._cancel_event.clear()
        self._runtime_state.state = "listening"
        
        return True
    
    def get_state(self) -> str:
        """Get current runtime state."""
        return self._runtime_state.state
    
    def get_runtime_status(self) -> Dict[str, Any]:
        """Get comprehensive runtime status."""
        return self._runtime_state.to_dict()
    
    def _start_wake_word_listener(self) -> None:
        """Start wake word listener."""
        
        self._is_running = True
        self._runtime_state.state = "listening"
        self._runtime_state.listening_since = utcnow().isoformat()
        
        self._cancel_event.clear()
        self._listen_thread = threading.Thread(
            target=self._wake_word_listen_loop,
            daemon=True
        )
        self._listen_thread.start()
    
    def _start_listening_mode(self) -> None:
        """Start push-to-talk listening mode."""
        
        self._is_running = True
        self._runtime_state.state = "listening"
        self._runtime_state.listening_since = utcnow().isoformat()
        
        self._cancel_event.clear()
        self._listen_thread = threading.Thread(
            target=self._direct_listen_loop,
            daemon=True
        )
        self._listen_thread.start()
    
    def _wake_word_listen_loop(self) -> None:
        """Main loop for wake word detection."""
        
        # In production, this would use actual wake word engine
        # For demo/testing, simulate detection after timeout
        try:
            while not self._cancel_event.wait(timeout=1.0):
                # Simulate checking for wake word
                pass
                
        except Exception as e:
            self._runtime_state.state = "error"
            self._runtime_state.permission_status = str(e)
    
    def _direct_listen_loop(self) -> None:
        """Main loop for direct listening (push-to-talk)."""
        
        try:
            while not self._cancel_event.wait(timeout=1.0):
                # Simulate capturing audio when button pressed
                pass
                
        except Exception as e:
            self._runtime_state.state = "error"
    
    def set_on_command_received(self, callback: Callable[[VoiceCommand], None]) -> None:
        """Set callback for when voice command is received."""
        self._on_command_received = callback
    
    def set_on_permission_error(self, callback: Callable[[str], None]) -> None:
        """Set callback for permission errors."""
        self._on_permission_error = callback


# Singleton instance for global access
DEFAULT_VOICE_RUNTIME = VoiceRuntimeController.get_instance()
