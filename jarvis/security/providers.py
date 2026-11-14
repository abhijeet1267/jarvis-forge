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
        """Check if a provider/model can handle all required capabilities.

        Conservative by design and **not** wired as a request gate: this reads
        only the *declared* profiles (``DEFAULT_CAPABILITIES`` plus anything
        explicitly ``update_model_capabilities``-ed). A provider that is
        genuinely serving traffic may have no declared profile here, so this
        can report a working model as unable to chat -- which is why a gate
        built on it would refuse real requests, and why the live path never
        consults it for that. Real, observed capability evidence is reported
        separately by :meth:`capability_report`, from the route-health ledger
        that records every actual completion.
        """
        for cap in required_capabilities:
            if not self.supports_capability(provider_id, model, cap):
                return False
        return True

    def capability_report(self, provider_id: str, model: str) -> Dict[str, Any]:
        """What a provider/model's capability is, **as evidenced**, not as assumed.

        A capability has three honest states, and conflating them is what made
        this subsystem dangerous to wire up as a gate:

        * ``verified`` -- a completion to this exact provider/model succeeded
          and was recorded in the route-health ledger (real runtime evidence,
          not a claim). This is the only tier that counts as proof the model
          can actually do the thing.
        * ``declared`` -- a static profile names the capability but no success
          has been observed for it. It is an expectation, not evidence.
        * ``unknown`` -- neither: no success observed, no profile claims it.

        Only ``TEXT`` is derivable from evidence, because a successful
        completion is exactly a successful text request. Everything else has
        no completion to prove it, so it stays declared/unknown rather than
        being inferred -- claiming a model can do tool-calling or vision just
        because it answered once would be the "name in a registry means the
        model can do it" error this report exists to prevent.
        """
        from ..gateway.route_health import get_route_health

        entry = get_route_health().snapshot(provider_id, model)
        last_success = entry.get("last_success_at")
        profile = self.get_capabilities(provider_id, model)

        # The declared tier is everything the static profile asserts. Some of it
        # is stored in the capability dict; the rest as boolean fields
        # (tool-calling, streaming) or the context window. All of it is a
        # *declaration*, not evidence -- none of it becomes "verified" here.
        declared = dict(profile.capabilities)
        if profile.supports_tools:
            declared.setdefault(CapabilityType.TOOL_CALLING, "supported")
        if profile.supports_streaming:
            declared.setdefault(CapabilityType.STREAMING, "supported")
        if profile.context_window_tokens > 0:
            declared.setdefault(CapabilityType.LONG_CONTEXT, "supported")

        report: Dict[str, Any] = {
            "provider_id": provider_id,
            "model": model,
            "capabilities": {},
            "last_success_at": last_success,
        }
        for cap in CapabilityType:
            if cap is CapabilityType.TEXT and last_success:
                report["capabilities"][cap.value] = "verified"
            elif cap in declared:
                report["capabilities"][cap.value] = "declared"
            else:
                report["capabilities"][cap.value] = "unknown"
        return report
    
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
        """macOS Keychain-based credential storage.

        `keyring.set_password` / `delete_password` return ``None`` on success and *raise*
        on failure -- so success is the absence of an exception, not a truthy return value.
        Reporting the raw ``None`` as the result (the old behaviour) made a successful store
        read as a failure and 500'd, and a successful delete read as 404. The store is the
        source of truth for "did it persist": return True only when the backend accepted it.
        """

        SERVICE_PREFIX = "vantage.ai."

        @classmethod
        def service(cls, provider_id: str) -> str:
            return f"{cls.SERVICE_PREFIX}{provider_id}"

        @classmethod
        def store(cls, provider_id: str, secret: str) -> bool:
            """Store a secret. True only if the backend accepted it."""
            try:
                keyring.set_password(cls.service(provider_id), "credential", secret)
                return True
            except Exception:
                return False

        @classmethod
        def retrieve(cls, provider_id: str) -> Optional[str]:
            """The stored secret, or None (not stored, or backend unavailable)."""
            try:
                return keyring.get_password(cls.service(provider_id), "credential")
            except Exception:
                return None

        @classmethod
        def delete(cls, provider_id: str) -> bool:
            """Delete the secret. True if it was present and is gone now."""
            try:
                keyring.delete_password(cls.service(provider_id), "credential")
                return True
            except keyring.errors.PasswordDeleteError:
                # No such item to delete -- not an error the caller must act on as "failed",
                # but a 404 signal the route can use to say "there was nothing stored".
                return False
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

#: Display metadata over the *authoritative* provider set. The adapters that actually
#: exist are in ``gateway.providers.PROVIDERS``; this module must not maintain a parallel
#: list that can drift (it once listed only "openai", so the settings page hid a real
#: Gemini adapter). Human-facing names live here; machine-facing facts live with the adapter.
_DISPLAY = {
    "openai": ("OpenAI", ProviderType.OPENAI),
    "gemini": ("Google Gemini", ProviderType.GOOGLE_GEMINI),
    "groq": ("Groq", ProviderType.OPENAI),
    "9router": ("9Router (local gateway)", ProviderType.OPENAI),
    "openai_compatible": ("OpenAI-compatible (custom)", ProviderType.OPENAI),
    "chat2api": ("Chat2API pool (local gateway)", ProviderType.OPENAI),
}


def list_providers() -> List[ProviderSpec]:
    """The supported providers, derived from the live adapters.

    Built from ``gateway.providers.PROVIDERS`` so a new adapter automatically appears
    here (and in the settings page / credential table) without a second edit that can
    be forgotten -- the source of truth is one dictionary, not two.
    """
    from ..gateway.providers import PROVIDERS

    specs: List[ProviderSpec] = []
    for provider_id, cls in PROVIDERS.items():
        display_name, ptype = _DISPLAY.get(provider_id, (provider_id.capitalize(), ProviderType.OPENAI))
        specs.append(ProviderSpec(
            id=provider_id,
            display_name=display_name,
            type=ptype,
            models=list(getattr(cls, "SUPPORTED_MODELS", []) or []),
            capabilities={"chat": True, "embeddings": provider_id == "openai"},
        ))
    return specs


def get_provider(provider_id: str) -> Optional[ProviderSpec]:
    """Get a provider by ID."""
    return next((p for p in list_providers() if p.id == provider_id), None)


def register_provider(spec: ProviderSpec) -> None:
    """No-op kept for API compatibility: the registry is the gateway's adapters, not a
    mutable local list. A hand-registered provider without an adapter would be a
    selection the assistant cannot call, which this project refuses rather than accepts."""
    return None


# --- Database operations ---

def create_provider_table():
    """Ensure the provider tables exist, and seed them from the live adapter registry.

    The seed is the authoritative ``PROVIDERS`` from the gateway (the providers that
    actually have an adapter). Without it, ``ai_providers`` is empty and the
    ``ai_credentials.provider_id`` foreign key has nothing to reference, so the very
    first credential store fails with an IntegrityError the moment secure storage
    actually works. Seeding is idempotent (INSERT OR IGNORE) and never overwrites a
    row the user has already changed.
    """
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

    _seed_providers(db)


def _seed_providers(db) -> None:
    """Insert one row per provider that has a real adapter, only if it is not already there.

    Read from the gateway's ``PROVIDERS`` so the DB cannot drift from the adapters that
    actually exist. The type string is mapped onto ``ProviderType``; a provider with no
    matching type is still recorded (as a generic cloud provider) so a future adapter is
    not blocked on the enum catching up.
    """
    try:
        from ..gateway.providers import PROVIDERS
    except Exception:
        return  # the gateway is the source of truth; if it is unavailable, do not seed

    for provider_id, cls in PROVIDERS.items():
        already = db.query("SELECT 1 FROM ai_providers WHERE provider_id = ?", (provider_id,))
        if already:
            continue
        models = list(getattr(cls, "SUPPORTED_MODELS", []) or [])
        # Map the live adapter id onto the ProviderType enum. This is an informational
        # column; a provider with no matching enum member is still recorded so a future
        # adapter is not blocked on the enum catching up.
        type_str = _provider_type_string(provider_id)
        db.execute(
            """INSERT OR IGNORE INTO ai_providers
               (provider_id, display_name, type, enabled, verified, models, capabilities)
               VALUES (?, ?, ?, 1, 0, ?, '{}')""",
            (provider_id, provider_id.capitalize(), type_str,
             json.dumps(models, default=str)),
        )


def _provider_type_string(provider_id: str) -> str:
    """The ``ai_providers.type`` value for a live adapter id, without pretending a
    mismatch: gemini is a Google provider, not an OpenAI one."""
    mapping = {
        "openai": ProviderType.OPENAI,
        "gemini": ProviderType.GOOGLE_GEMINI,
    }
    return mapping.get(provider_id, ProviderType.OPENAI).value


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