"""AI provider management — secure credential storage, verification status tracking, and multimodal capability routing (Phase 74)."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..config import get_settings
from ..db.database import get_db


# --- Capability Registry (Phase 74) ---

class CapabilityType(Enum):
    """Types of capabilities providers/models can support."""
    
    TEXT = "text"                    # Basic text chat/generation
    VISION = "vision"                 # Image analysis  
    IMAGE_INPUT = "image_input"       # Accept uploaded images
    PDF_INPUT = "pdf_input"           # Accept PDF files
    DOCUMENT_INPUT = "document_input" # Accept documents (docx, etc.)
    AUDIO_INPUT = "audio_input"       # Accept audio files
    AUDIO_TRANSCRIPTION = "audio_transcription"  # Speech-to-text
    VOICE_OUTPUT = "voice_output"     # Text-to-speech
    STRUCTURED_OUTPUT = "structured_output"      # JSON/schema output
    TOOL_CALLING = "tool_calling"     # Can invoke tools/actions
    LONG_CONTEXT = "long_context"     # Context window > standard
    STREAMING = "streaming"           # Response streaming


@dataclass  
class ProviderModelCapabilities:
    """Complete capability profile for a provider/model."""
    
    provider_id: str
    model: str
    
    capabilities: Dict[CapabilityType, str] = field(default_factory=dict)
    # Maps CapabilityType -> "supported"/"unsupported"/"unknown"
    
    context_window_tokens: int = 0
    max_attachment_size_bytes: int = 0
    supports_streaming: bool = False
    supports_tools: bool = False
    
    @property
    def text_supported(self) -> bool:
        return self.capabilities.get(CapabilityType.TEXT) == "supported"
    
    @property
    def vision_supported(self) -> bool:
        return self.capabilities.get(CapabilityType.VISION) == "supported"
    
    @property
    def pdf_supported(self) -> bool:
        return self.capabilities.get(CapabilityType.PDF_INPUT) == "supported"
    
    @property
    def image_supported(self) -> bool:
        return self.capabilities.get(CapabilityType.IMAGE_INPUT) == "supported"
    
    @property
    def audio_supported(self) -> bool:
        return self.capabilities.get(CapabilityType.AUDIO_INPUT) == "supported"
    
    @property
    def transcription_supported(self) -> bool:
        return self.capabilities.get(CapabilityType.AUDIO_TRANSCRIPTION) == "supported"
    
    @property
    def voice_output_supported(self) -> bool:
        return self.capabilities.get(CapabilityType.VOICE_OUTPUT) == "supported"
    
    @property
    def tool_support(self) -> bool:
        return self.supports_tools


# Default capability profiles - updated dynamically as connections are verified
DEFAULT_CAPABILITIES: Dict[str, Dict[str, str]] = {
    # OpenAI GPT-4o
    "openai:gpt-4o": {
        "text": "supported",
        "vision": "supported", 
        "image_input": "supported",
        "pdf_input": "supported",
        "structured_output": "supported",
        "tool_calling": "supported",
        "streaming": "supported",
        "context_window": 128000,
    },
    "openai:gpt-4o-mini": {
        "text": "supported",
        "vision": "supported",
        "image_input": "supported",
        "pdf_input": "supported", 
        "structured_output": "supported",
        "tool_calling": "supported",
        "streaming": "supported",
        "context_window": 128000,
    },
    "openai:gpt-4-turbo": {
        "text": "supported",
        "vision": "supported",
        "image_input": "supported",
        "tool_calling": "supported",
        "streaming": "supported",
        "context_window": 128000,
    },
    "openai:gpt-4": {
        "text": "supported",
        "tool_calling": "supported",
        "streaming": "supported",
        "context_window": 8192,
    },
    # Gemini models
    "gemini:gemini-1.5-pro": {
        "text": "supported",
        "vision": "supported",
        "image_input": "supported",
        "pdf_input": "supported",
        "structured_output": "supported",
        "tool_calling": "supported",
        "streaming": "supported",
        "context_window": 1000000,  # 1M tokens
    },
    "gemini:gemini-1.5-flash": {
        "text": "supported",
        "vision": "supported",
        "image_input": "supported",
        "pdf_input": "supported",
        "structured_output": "supported",
        "streaming": "supported",
        "context_window": 1000000,  # 1M tokens
    },
}


class CapabilityRegistry:
    """Singleton registry of provider/model capabilities (Phase 74)."""
    
    _instance: Optional["CapabilityRegistry"] = None
    _initialized = False
    
    def __new__(cls) -> "CapabilityRegistry":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self) -> None:
        if self._initialized:
            return
        self._models: Dict[str, ProviderModelCapabilities] = {}
        self._load_defaults()
        self._initialized = True
    
    def _load_defaults(self) -> None:
        """Load default capability profiles from DEFAULT_CAPABILITIES."""
        for key, caps in DEFAULT_CAPABILITIES.items():
            parts = key.split(":")
            if len(parts) != 2:
                continue
            
            provider_id, model = parts
            
            cap_dict = ProviderModelCapabilities(
                provider_id=provider_id,
                model=model,
            )
            
            for cap_type, status in caps.items():
                if cap_type == "context_window":
                    cap_dict.context_window_tokens = caps.get("context_window", 0)
                elif cap_type == "streaming":
                    cap_dict.supports_streaming = status == "supported"
                elif cap_type == "tool_calling":
                    cap_dict.supports_tools = status == "supported"
                else:
                    try:
                        cap_type_enum = CapabilityType(cap_type)
                        cap_dict.capabilities[cap_type_enum] = status
                    except ValueError:
                        pass
            
            self._models[key] = cap_dict
    
    def get_capabilities(self, provider_id: str, model: str) -> ProviderModelCapabilities:
        """Get capabilities for a specific provider/model combination."""
        key = f"{provider_id}:{model}"
        
        if key not in self._models:
            # Create default entry for unknown combination
            self._models[key] = ProviderModelCapabilities(
                provider_id=provider_id,
                model=model,
            )
        
        return self._models[key]
    
    def supports_capability(self, provider_id: str, model: str, 
                           capability: CapabilityType) -> bool:
        """Check if a provider/model supports a specific capability."""
        caps = self.get_capabilities(provider_id, model)
        return caps.capabilities.get(capability) == "supported"
    
    def can_handle_request(self, provider_id: str, model: str,
                          required_capabilities: List[CapabilityType]) -> bool:
        """Check if a provider/model can handle all required capabilities."""
        for cap in required_capabilities:
            if not self.supports_capability(provider_id, model, cap):
                return False
        return True
    
    def update_model_capabilities(self, provider_id: str, model: str,
                                 capabilities: Dict[CapabilityType, str],
                                 context_window: int = 0,
                                 supports_streaming: bool = False,
                                 supports_tools: bool = False) -> None:
        """Update capabilities for a provider/model after verification."""
        key = f"{provider_id}:{model}"
        
        if key in self._models:
            caps = self._models[key]
            caps.capabilities.update(capabilities)
            if context_window > 0:
                caps.context_window_tokens = context_window
            caps.supports_streaming = supports_streaming
            caps.supports_tools = supports_tools
        else:
            new_caps = ProviderModelCapabilities(
                provider_id=provider_id,
                model=model,
                capabilities=capabilities,
                context_window_tokens=context_window,
                supports_streaming=supports_streaming,
                supports_tools=supports_tools,
            )
            self._models[key] = new_caps
    
    def find_compatible_providers(self, required_capabilities: List[CapabilityType],
                                 configured_providers: List[Dict[str, Any]],
                                 free_first: bool = True,
                                 allow_paid_fallback: bool = False) -> List[Dict[str, Any]]:
        """Find configured providers that can handle all required capabilities."""
        compatible = []
        
        for provider_info in configured_providers:
            provider_id = provider_info["provider_id"]
            
            # Try each known model for this provider
            for model in provider_info.get("models", []):
                if self.can_handle_request(provider_id, model, required_capabilities):
                    compatible.append({
                        "provider_id": provider_id,
                        "model": model,
                        "verified": provider_info.get("verified", False),
                        "free_or_plan": provider_info.get("status") != "paid_api",
                        "capabilities": required_capabilities,
                    })
        
        # Sort: verified/free first, then paid
        def sort_key(item):
            score = 0
            if item["verified"]:
                score += 1000
            if item["free_or_plan"] and free_first:
                score += 500
            if not allow_paid_fallback and not item["free_or_plan"]:
                score -= 10000
            return score
        
        compatible.sort(key=sort_key, reverse=True)
        
        # If no free providers available but paid allowed, include paid
        if free_first and not allow_paid_fallback:
            compatible = [p for p in compatible if p["free_or_plan"]]
        
        return compatible
    
    def get_recommendation_reason(self, provider_id: str, model: str,
                                  required_capabilities: List[CapabilityType]) -> str:
        """Generate an explanation for why this provider was selected."""
        reasons = []
        
        for cap in required_capabilities:
            reason_map = {
                CapabilityType.VISION: "image input support",
                CapabilityType.PDF_INPUT: "PDF document support",
                CapabilityType.TEXT: "basic text responses",
                CapabilityType.TOOL_CALLING: "tool invocation support",
            }
            if cap in reason_map:
                reasons.append(reason_map[cap])
        
        return f"supports: {', '.join(reasons)}"


def get_registry() -> CapabilityRegistry:
    """Get or create the singleton capability registry."""
    if CapabilityRegistry._instance is None:
        CapabilityRegistry()
    return CapabilityRegistry._instance


# Continue with existing code...


class ProviderType(Enum):
    """Supported AI provider types."""
    OPENAI = "openai"
    ANTHROPIC = "anthropic"  # Not implemented yet
    GOOGLE_GEMINI = "google_gemini"  # Not implemented yet
    OLLAMA = "ollama"  # Not implemented yet


@dataclass
class ProviderSpec:
    """Metadata about a supported provider."""
    id: str
    display_name: str
    type: ProviderType
    enabled: bool = True
    verified: bool = False
    last_verified_at: Optional[datetime] = None
    last_error: Optional[str] = None
    models: List[str] = field(default_factory=list)
    capabilities: Dict[str, bool] = field(default_factory=dict)
    
    @property
    def configured(self) -> bool:
        return self.verified or any([self.capabilities.get('chat', False)])


@dataclass 
class CredentialRecord:
    """Secure credential reference record."""
    id: str
    provider_id: str
    created_at: float
    updated_at: float
    last_used_at: Optional[float] = None
    last_verified_at: Optional[float] = None
    status: str = "unknown"  # active, revoked, expired


# --- Keychain credential storage ---

try:
    import keyring
    
    class KeychainStorage:
        """macOS Keychain-based credential storage."""
        
        SERVICE_PREFIX = "vantage.ai."
        
        @classmethod
        def store(cls, provider_id: str, secret: str) -> bool:
            """Store a secret in the keychain."""
            try:
                service = f"{cls.SERVICE_PREFIX}{provider_id}"
                return keyring.set_password(service, "credential", secret)
            except Exception:
                return False
        
        @classmethod
        def retrieve(cls, provider_id: str) -> Optional[str]:
            """Retrieve a secret from the keychain."""
            try:
                service = f"{cls.SERVICE_PREFIX}{provider_id}"
                return keyring.get_password(service, "credential")
            except Exception:
                return None
        
        @classmethod
        def delete(cls, provider_id: str) -> bool:
            """Delete a secret from the keychain."""
            try:
                service = f"{cls.SERVICE_PREFIX}{provider_id}"
                return keyring.delete_password(service, "credential")
            except Exception:
                return False
                
except ImportError:
    class KeychainStorage:
        """Fallback for systems without keyring."""
        
        @staticmethod
        def store(*args, **kwargs) -> bool:
            return False
        
        @staticmethod
        def retrieve(*args, **kwargs) -> Optional[str]:
            return None
        
        @staticmethod
        def delete(*args, **kwargs) -> bool:
            return False


# --- Provider registry ---

_providers: Dict[str, ProviderSpec] = {
    "openai": ProviderSpec(
        id="openai",
        display_name="OpenAI",
        type=ProviderType.OPENAI,
        models=["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-4"],
        capabilities={"chat": True, "embeddings": False},
    ),
}


def list_providers() -> List[ProviderSpec]:
    """List all supported providers."""
    return list(_providers.values())


def get_provider(provider_id: str) -> Optional[ProviderSpec]:
    """Get a provider by ID."""
    return _providers.get(provider_id)


def register_provider(spec: ProviderSpec) -> None:
    """Register a new provider (for extensibility)."""
    _providers[spec.id] = spec


# --- Database operations ---

def create_provider_table():
    """Ensure providers table exists."""
    db = get_db()
    
    db.execute("""
    CREATE TABLE IF NOT EXISTS ai_providers (
        provider_id TEXT PRIMARY KEY,
        display_name TEXT NOT NULL,
        type TEXT NOT NULL,
        enabled INTEGER DEFAULT 1,
        verified INTEGER DEFAULT 0,
        last_verified_at TEXT,
        last_error TEXT,
        models TEXT DEFAULT '[]',
        capabilities TEXT DEFAULT '{}',
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """)
    
    db.execute("""
    CREATE TABLE IF NOT EXISTS ai_credentials (
        credential_id TEXT PRIMARY KEY,
        provider_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        last_used_at REAL,
        last_verified_at REAL,
        FOREIGN KEY (provider_id) REFERENCES ai_providers(provider_id)
    )
    """)


def get_credentials(provider_id: str) -> List[CredentialRecord]:
    """Get credentials for a provider."""
    db = get_db()
    rows = db.query(
        "SELECT * FROM ai_credentials WHERE provider_id = ? ORDER BY created_at DESC",
        (provider_id,)
    )
    return [CredentialRecord(
        id=row["credential_id"],
        provider_id=row["provider_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_used_at=row["last_used_at"],
        last_verified_at=row["last_verified_at"],
        status=row["status"]
    ) for row in rows]


def save_credential_ref(provider_id: str, status: str = "active") -> str:
    """Create a new credential reference record."""
    import secrets
    credential_id = f"CRED-{secrets.token_urlsafe(8)}"
    
    now = time.time()
    
    db = get_db()
    db.execute(
        """INSERT INTO ai_credentials 
           (credential_id, provider_id, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?)""",
        (credential_id, provider_id, status, now, now)
    )
    
    return credential_id


def update_credential_status(credential_id: str, status: str) -> bool:
    """Update a credential's status."""
    db = get_db()
    db.execute(
        "UPDATE ai_credentials SET status = ?, updated_at = ? WHERE credential_id = ?",
        (status, time.time(), credential_id)
    )
    return True


def invalidate_old_credentials(provider_id: str) -> int:
    """Invalidate old credentials for a provider when a new one is added."""
    db = get_db()
    db.execute(
        "UPDATE ai_credentials SET status = 'revoked', updated_at = ? WHERE provider_id = ? AND status = 'active'",
        (time.time(), provider_id)
    )
    
    cursor = db.execute(
        "SELECT COUNT(*) as count FROM ai_credentials WHERE provider_id = ? AND status = 'revoked'",
        (provider_id,)
    )
    return cursor.fetchone()["count"]


def clear_providers_cache():
    """Clear cached provider states from config."""
    settings = get_settings()
    settings._llm_model = None
    settings._llm_enabled = None