"""Minimal security tests for Phase 72 AI provider integration."""

from __future__ import annotations

import re
from pathlib import Path

import pytest


def test_ai_connections_html_has_no_hardcoded_secrets():
    """The AI connections HTML page must not have any hardcoded API keys."""
    # dashboard/ai_connections.html relative to tests directory
    html_path = Path(__file__).resolve().parent.parent / "dashboard" / "ai_connections.html"
    assert html_path.exists(), f"AI connections page not found at {html_path}"
    
    html_content = html_path.read_text()
    
    api_key_patterns = [
        r'sk-[a-zA-Z0-9_-]{20,}',  # OpenAI-style keys
        r'AIza[a-zA-Z0-9_-]{35,}',  # Google-style keys  
        r'Bearer [a-zA-Z0-9_-]+',   # Bearer tokens
    ]
    
    for pattern in api_key_patterns:
        matches = re.findall(pattern, html_content)
        assert not matches, f"Hardcoded secret pattern found: {pattern}"


def test_dashboard_contains_ai_connections_link():
    """Dashboard should link to AI Connections settings page."""
    dashboard_path = Path(__file__).resolve().parent.parent / "dashboard" / "index.html"
    content = dashboard_path.read_text()
    
    assert 'href="/ai-connections"' in content or "ai-connections" in content.lower(), \
        f"Dashboard missing AI connections navigation at {dashboard_path}"


def test_openai_provider_registered_with_correct_url():
    """OpenAI provider registered with correct official endpoint."""
    from jarvis.gateway.providers import OpenAIProvider, PROVIDERS
    
    assert "openai" in PROVIDERS, "OpenAI provider not registered"
    assert hasattr(OpenAIProvider, 'DEFAULT_BASE_URL'), "Missing DEFAULT_BASE_URL"
    assert OpenAIProvider.DEFAULT_BASE_URL == "https://api.openai.com/v1", \
        "OpenAI using incorrect base URL"


def test_keychain_storage_imported():
    """Keychain storage wrapper imported and available."""
    from jarvis.security.providers import KeychainStorage
    
    assert hasattr(KeychainStorage, 'store'), "Keychain storage missing store method"
    assert hasattr(KeychainStorage, 'retrieve'), "Keychain storage missing retrieve method"
    assert hasattr(KeychainStorage, 'delete'), "Keychain storage missing delete method"


def test_provider_api_routes_exist():
    """API routes for providers are registered."""
    from jarvis.api.app import app
    
    route_paths = [route.path for route in app.routes]
    
    expected_routes = [
        "/ai/providers",
        "/ai/providers/{provider_id}",
        "/ai/providers/{provider_id}/test", 
        "/ai/providers/{provider_id}/store",
        "/ai/providers/{provider_id}/credentials",
        "/ai/capabilities",
        "/ai/verify-all",
    ]
    
    for route in expected_routes:
        found = any(route in path for path in route_paths)
        assert found, f"Route {route} not found"
