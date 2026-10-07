"""AI provider management — secure credential storage and verification status tracking."""

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