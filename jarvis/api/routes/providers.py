"""API routes for AI provider / credential management."""

from __future__ import annotations

import json
import secrets
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query

from ..config import get_settings
from ..gateway.providers import PROVIDERS, OpenAIProvider, ProviderUnknown, provider_capabilities
from ..logging_setup import get_logger
from ..security.providers import (
    create_provider_table,
    get_credentials,
    list_providers,
    save_credential_ref,
    update_credential_status,
    KeychainStorage,
)

log = get_logger("api.providers")

router = APIRouter(prefix="/api/ai", tags=["AI providers"])


@router.get("/providers", response_model=List[Dict[str, Any]])
async def list_ai_providers() -> List[Dict[str, Any]]:
    """List all available AI providers with their status."""
    from datetime import datetime
    
    # Initialize database tables if needed
    create_provider_table()
    
    settings = get_settings()
    results = []
    
    for spec in list_providers():
        provider_cls = PROVIDERS.get(spec.id)
        if not provider_cls:
            continue
            
        try:
            provider = provider_cls(settings)
            
            # Check if configured by attempting to see if key exists in keychain
            configured = False
            verified = False
            last_verified = None
            last_error = None
            
            # Get credential reference from DB
            creds = get_credentials(spec.id)
            active_creds = [c for c in creds if c.status == "active"]
            if active_creds:
                configured = True
                
                # Try to verify
                secret = KeychainStorage.retrieve(spec.id)
                if secret:
                    temp_settings = type('Settings', (), {
                        'llm_api_key': secret,
                        'llm_base_url': provider.DEFAULT_BASE_URL,
                        'ai_provider': spec.type.value
                    })()
                    
                    try:
                        test_provider = provider_cls(temp_settings)
                        result = test_provider.verify_connection(timeout=2.0)
                        if result.success:
                            verified = True
                            last_verified = datetime.now().isoformat()
                            # Update credential verification time
                            if active_creds:
                                update_credential_status(active_creds[0].id, "active")
                    except Exception as e:
                        last_error = str(e)[:200]
                        
        except Exception as exc:
            log.warning(f"error checking provider {spec.id}", extra_fields={"error": str(exc)})
        
        results.append({
            "provider_id": spec.id,
            "display_name": spec.display_name,
            "type": spec.type.value,
            "enabled": spec.enabled,
            "configured": configured,
            "verified": verified,
            "last_verified_at": last_verified or "",
            "last_error": last_error or "",
            "models": spec.models if hasattr(spec, 'models') else [],
            "capabilities": spec.capabilities if hasattr(spec, 'capabilities') else {},
        })
    
    return results


@router.get("/providers/{provider_id}")
async def get_provider_details(provider_id: str) -> Dict[str, Any]:
    """Get detailed information about a specific provider."""
    from datetime import datetime
    
    create_provider_table()
    
    spec = next((s for s in list_providers() if s.id == provider_id), None)
    if not spec:
        raise HTTPException(status_code=404, detail=f"Provider {provider_id} not found")
    
    provider_cls = PROVIDERS.get(provider_id)
    if not provider_cls:
        raise HTTPException(status_code=404, detail=f"Implementation missing for {provider_id}")
    
    settings = get_settings()
    
    configured = False
    verified = False
    last_verified = None
    last_error = None
    
    # Get credential reference from DB
    creds = get_credentials(provider_id)
    active_creds = [c for c in creds if c.status == "active"]
    if active_creds:
        configured = True
        
        # Try to verify
        secret = KeychainStorage.retrieve(provider_id)
        if secret:
            temp_settings = type('Settings', (), {
                'llm_api_key': secret,
                'llm_base_url': getattr(provider_cls, 'DEFAULT_BASE_URL', 'https://api.openai.com/v1'),
                'ai_provider': spec.type.value
            })()
            
            try:
                test_provider = provider_cls(temp_settings)
                result = test_provider.verify_connection(timeout=2.0)
                if result.success:
                    verified = True
                    last_verified = datetime.now().isoformat()
                    update_credential_status(active_creds[0].id, "active")
            except Exception as e:
                last_error = str(e)[:200]
    
    return {
        "provider_id": spec.id,
        "display_name": spec.display_name,
        "type": spec.type.value,
        "enabled": spec.enabled,
        "configured": configured,
        "verified": verified,
        "last_verified_at": last_verified or "",
        "last_error": last_error or "",
        "models": spec.models if hasattr(spec, 'models') else [],
        "capabilities": spec.capabilities if hasattr(spec, 'capabilities') else {},
    }


@router.post("/providers/{provider_id}/test")
async def test_provider_credentials(provider_id: str, api_key: str = Query(...)) -> Dict[str, Any]:
    """Test an API key without storing it permanently."""
    from datetime import datetime
    
    provider_cls = PROVIDERS.get(provider_id)
    if not provider_cls:
        raise HTTPException(status_code=404, detail=f"Provider {provider_id} not found")
    
    # Test the credentials
    temp_settings = type('Settings', (), {
        'llm_api_key': api_key,
        'llm_base_url': getattr(provider_cls, 'DEFAULT_BASE_URL', 'https://api.openai.com/v1'),
        'ai_provider': provider_id
    })()
    
    try:
        test_provider = provider_cls(temp_settings)
        result = test_provider.verify_connection(timeout=5.0)
        
        return {
            "success": result.success,
            "message": result.error if not result.success else "Connection verified successfully",
            "timestamp": datetime.now().isoformat(),
            "notes": result.notes or {}
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Testing failed: {str(exc)}")


@router.post("/providers/{provider_id}/store")
async def store_provider_credentials(provider_id: str, 
                                    api_key: str = Query(...)) -> Dict[str, Any]:
    """Store credentials securely in keychain."""
    from datetime import datetime
    
    provider_cls = PROVIDERS.get(provider_id)
    if not provider_cls:
        raise HTTPException(status_code=404, detail=f"Provider {provider_id} not found")
    
    # Validate provider name format
    if not api_key or len(api_key) < 10:
        raise HTTPException(status_code=400, detail="Invalid API key format")
    
    # Store in keychain
    stored = KeychainStorage.store(provider_id, api_key)
    if not stored:
        raise HTTPException(status_code=500, detail="Failed to store credentials securely")
    
    # Create credential reference
    cred_id = save_credential_ref(provider_id, "active")
    
    # Invalidate old credentials
    invalidated = 0
    
    return {
        "success": True,
        "credential_id": cred_id,
        "message": "Credentials stored securely",
        "timestamp": datetime.now().isoformat(),
        "old_credentials_revoked": invalidated,
    }


@router.delete("/providers/{provider_id}/credentials")
async def delete_provider_credentials(provider_id: str) -> Dict[str, Any]:
    """Delete stored credentials."""
    from datetime import datetime
    
    # Delete from keychain
    deleted = KeychainStorage.delete(provider_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Credential not found")
    
    # Mark all references as revoked
    db_creds = get_credentials(provider_id)
    for cred in db_creds:
        update_credential_status(cred.id, "revoked")
    
    return {
        "success": True,
        "message": "Credentials deleted successfully",
        "timestamp": datetime.now().isoformat(),
    }


@router.get("/capabilities")
async def get_ai_capabilities() -> Dict[str, Any]:
    """Report which tasks go through which provider."""
    return provider_capabilities()


@router.post("/verify-all")
async def verify_all_providers() -> Dict[str, Any]:
    """Verify connection for all configured providers."""
    from datetime import datetime
    
    settings = get_settings()
    results = {}
    
    for provider_id, provider_cls in PROVIDERS.items():
        try:
            provider = provider_cls(settings)
            if provider.configured():
                result = provider.verify_connection(timeout=3.0)
                results[provider_id] = {
                    "verified": result.success,
                    "error": result.error if not result.success else None,
                    "notes": result.notes or {},
                }
            else:
                results[provider_id] = {"verified": False, "error": "not configured"}
        except Exception as e:
            results[provider_id] = {"verified": False, "error": str(e)}
    
    return {
        "timestamp": datetime.now().isoformat(),
        "results": results,
    }