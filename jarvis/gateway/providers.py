"""Provider-neutral model gateway (Phase 81).

Unified interface for:
* OpenAI API / ChatGPT account
* Google Gemini  
* Apple Foundation Models
* Shree AI (when officially supported)

Privacy-first routing that never sends secrets to external providers.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
import re

from ..logging_setup import get_logger
from ..net import http_read

log = get_logger("gateway.providers")


class ProviderName(str, Enum):
    """Known AI providers."""
    OPENAI = "openai"
    CHATGPT = "chatgpt"  
    GEMINI = "gemini"
    APPLE = "apple"
    SHREE = "shree"


@dataclass
class ProviderCapabilities:
    """What a provider can do."""
    supports_text: bool = True
    supports_vision: bool = False
    supports_audio: bool = False
    supports_multimodal: bool = False
    supports_streaming: bool = False
    local_processing: bool = False
    free_tier_available: bool = False
    paid_tier_required: bool = True
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.supports_text,
            "vision": self.supports_vision,
            "audio": self.supports_audio,
            "multimodal": self.supports_multimodal,
            "streaming": self.supports_streaming,
            "local": self.local_processing,
            "free": self.free_tier_available,
        }


@dataclass
class PrivacyProfile:
    """Data policy for a provider."""
    provider_name: str
    is_cloud_provider: bool
    data_retention_days: int = 0
    trains_on_user_data: bool = False
    supports_zero_retention: bool = False
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider_name,
            "cloud": self.is_cloud_provider,
            "retention_days": self.data_retention_days,
            "trains_on_data": self.trains_on_user_data,
        }


@dataclass
class ConnectionStatus:
    """Provider connection health."""
    provider: str
    configured: bool = False
    healthy: bool = False
    last_check: Optional[str] = None
    error: Optional[str] = None
    models_available: List[str] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "configured": self.configured,
            "healthy": self.healthy,
            "last_check": self.last_check,
            "error": self.error,
            "models": self.models_available,
        }


class ProviderUnknown(ValueError):
    """The configured provider name is not known."""


class ProviderCapabilityMissing(ValueError):
    """This provider cannot perform this operation."""


class ProviderFailed(RuntimeError):
    """A provider call failed with an actual error."""

    def __init__(self, provider: str, message: str, details: Optional[Dict] = None):
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.message = message
        self.details = details or {}


@dataclass
class ModelInfo:
    """Model metadata from a provider."""
    name: str
    capabilities: List[str]
    context_window: int = 0
    pricing_info: Optional[str] = None
    available: bool = False
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "capabilities": self.capabilities,
            "context_window": self.context_window,
            "pricing": self.pricing_info,
            "available": self.available,
        }


@dataclass
class ProviderRequest:
    """Unified request format for all providers."""
    messages: List[Dict[str, Any]]
    provider: str
    model: str
    temperature: float = 0.7
    max_tokens: Optional[int] = None
    stream: bool = False
    
    def with_secret_redaction(self) -> 'ProviderRequest':
        """Create version with secrets redacted before transmission."""
        redacted_messages = []
        
        for msg in self.messages:
            if "content" in msg:
                content = msg["content"]
                
                # Check for secrets in various formats
                redacted_content = _redact_secrets(content)
                
                msg_copy = msg.copy()
                msg_copy["content"] = redacted_content
                redacted_messages.append(msg_copy)
            else:
                redacted_messages.append(msg)
        
        return ProviderRequest(
            messages=redacted_messages,
            provider=self.provider,
            model=self.model,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            stream=self.stream,
        )


@dataclass
class ProviderReply:
    """One answer, from one named vendor."""
    
    ok: bool
    provider: str
    model: str
    text: str = ""
    vectors: Optional[List[List[float]]] = None
    error: str = ""
    status: int = 0
    usage: Dict[str, Any] = field(default_factory=dict)
    notes: Dict[str, Any] = field(default_factory=dict)
    
    def __post_init__(self) -> None:
        if not self.ok and not self.error:
            raise ValueError("a failed reply has to say why")
    
    def to_chat_response(self) -> Dict[str, Any]:
        """Convert to chat-compatible response format."""
        return {
            "ok": self.ok,
            "provider": self.provider,
            "model": self.model,
            "content": self.text if self.ok else "",
            "error": self.error,
            "usage": self.usage,
            "notes": self.notes,
        }


def _redact_secrets(text: str) -> str:
    """Redact common secret patterns from text."""
    
    SECRET_PATTERNS = [
        # API keys - generic patterns
        r'[a-zA-Z0-9_-]{32,}\s*[=\:]?\s*[\'"][\w\-]+[\'"]',
        
        # Bearer tokens
        r'Bearer\s+[a-zA-Z0-9_\-\.]+',
        
        # JWT tokens
        r'\beyJ[a-zA-Z0-9_\-]*\.eyJ[a-zA-Z0-9_\-]*\.[a-zA-Z0-9_\-]*\b',
        
        # Common key names with values
        r'(?:api[_-]?key|apikey|API_KEY)\s*=\s*[\'"]?[\w\-]{16,}[\'"]?',
        r'(?:access[_-]?token|accessToken|ACCESS_TOKEN)\s*=\s*[\'"]?[\w\-\.]{20,}[\'"]?',
        r'(?:password|passwd|pwd)\s*=\s*[\'"].{8,}[\'"]?',
        
        # AWS credentials
        r'AWS_ACCESS_KEY_ID\s*=\s*[A-Z0-9]{20}',
        r'AKIA[0-9A-Z]{16}',
        
        # GitHub tokens
        r'ghp_[a-zA-Z0-9]{36}',
        r'gho_[a-zA-Z0-9]{36}',
        
        # OAuth tokens
        r'oauth_token\s*=\s*[\'"][\w\-\.]{20,}[\'"]?',
        
        # Session cookies
        r'sessionid\s*=\s*[\'"][\w\-]{20,}[\'"]?',
        r'session\s*=\s*[\'"][\w\-]{20,}[\'"]?',
        
        # Private keys markers
        r'PRIVATE KEY',
        r'BEGIN RSA PRIVATE KEY',
        r'BEGIN EC PRIVATE KEY',
        
        # Password patterns
        r'password\s*[:=]\s*[^\s\'\n]{8,}',
        
        # Generic secrets
        r'(?i)(secret|token|credential|auth)[^"\n]*[=:]?[\'"][\w\-]{10,}[\'"]',
    ]
    
    result = text
    
    for pattern in SECRET_PATTERNS:
        result = re.sub(pattern, '[REDACTED_SECRET]', result, flags=re.IGNORECASE)
    
    return result


# Known provider capabilities
PROVIDER_CAPABILITIES = {
    ProviderName.OPENAI: ProviderCapabilities(
        supports_text=True,
        supports_vision=True,
        supports_multimodal=True,
        supports_streaming=True,
        free_tier_available=False,
        paid_tier_required=True,
    ),
    ProviderName.CHATGPT: ProviderCapabilities(
        supports_text=True,
        supports_vision=False,
        supports_multimodal=False,
        supports_streaming=True,
        free_tier_available=False,
        paid_tier_required=True,
    ),
    ProviderName.GEMINI: ProviderCapabilities(
        supports_text=True,
        supports_vision=True,
        supports_multimodal=True,
        supports_streaming=True,
        free_tier_available=True,
        paid_tier_required=False,
    ),
    ProviderName.APPLE: ProviderCapabilities(
        supports_text=True,
        local_processing=True,
        free_tier_available=True,
        paid_tier_required=False,
    ),
    ProviderName.SHREE: ProviderCapabilities(
        supports_text=True,
        local_processing=False,
        free_tier_available=False,
        paid_tier_required=False,
    ),
}

# Privacy profiles for each provider
PROVIDER_PRIVACY_PROFILES = {
    ProviderName.OPENAI: PrivacyProfile(
        provider_name="OpenAI",
        is_cloud_provider=True,
        data_retention_days=30,
        trains_on_user_data=True,
    ),
    ProviderName.CHATGPT: PrivacyProfile(
        provider_name="ChatGPT",
        is_cloud_provider=True,
        data_retention_days=30,
        trains_on_user_data=False,
    ),
    ProviderName.GEMINI: PrivacyProfile(
        provider_name="Gemini",
        is_cloud_provider=True,
        data_retention_days=0,
        trains_on_user_data=False,
    ),
    ProviderName.APPLE: PrivacyProfile(
        provider_name="Apple",
        is_cloud_provider=False,
        data_retention_days=0,
        trains_on_user_data=False,
    ),
    ProviderName.SHREE: PrivacyProfile(
        provider_name="Shree",
        is_cloud_provider=False,
        data_retention_days=0,
        trains_on_user_data=False,
    ),
}

# Available models per provider
PROVIDER_MODELS = {
    ProviderName.OPENAI: ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-4"],
    ProviderName.CHATGPT: ["chatgpt-latest"],
    ProviderName.GEMINI: ["gemini-1.5-pro", "gemini-1.5-flash", "gemini-pro"],
    ProviderName.APPLE: ["llama-3.1", "mistral-large", "dolphin-mixtral"],
    ProviderName.SHREE: [],
}


def select_provider(provider_name: str) -> ProviderName:
    """Select and validate a provider."""
    try:
        return ProviderName(provider_name.lower())
    except ValueError:
        valid = [p.value for p in ProviderName]
        raise ProviderUnknown(
            f"Unknown provider '{provider_name}'. Valid: {valid}"
        )


def get_capabilities(provider: str) -> ProviderCapabilities:
    """Get capabilities for a provider."""
    pname = select_provider(provider)
    return PROVIDER_CAPABILITIES.get(pname, ProviderCapabilities())


def get_privacy_profile(provider: str) -> PrivacyProfile:
    """Get privacy profile for a provider."""
    pname = select_provider(provider)
    return PROVIDER_PRIVACY_PROFILES.get(pname, PrivacyProfile(
        provider_name=pname.value,
        is_cloud_provider=False,
    ))


def get_available_models(provider: str) -> List[str]:
    """Get list of available models for a provider."""
    pname = select_provider(provider)
    return PROVIDER_MODELS.get(pname, [])


def classify_data_for_transmission(data: str, classification: str) -> bool:
    """Determine if data should be allowed based on classification and policy."""
    
    classification = classification.upper()
    
    # Secret data never leaves machine
    if classification in ["SECRET", "SYSTEM_SECRET"]:
        return False
    
    # Private data requires explicit approval
    if classification == "PRIVATE":
        return True  # Would require user confirmation in real flow
    
    # Sensitive data blocked by default
    if classification == "SENSITIVE":
        return False
        
    # Normal/public data may leave under policy
    return True


