"""AI provider request path -- deterministic coverage.

Proves the /ai/chat/send path actually uses the selected provider's adapter with
the configured model, reports a real success verbatim, and reports a provider
failure honestly (never a fabricated success). The provider call is mocked so the
test is network-free and deterministic; a clearly-marked optional live test sits
at the bottom and skips itself unless a real key and network are present.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

from jarvis.gateway.providers import ProviderReply
from tests.conftest import TEST_TOKEN


@pytest.fixture
def client():
    from jarvis.api.app import app
    return TestClient(app, base_url="http://127.0.0.1:8765")


def _fake_provider(name: str, model: str, *, ok: bool, text: str = "", error: str = ""):
    """A stand-in adapter whose async chat() records what it was asked and
    returns a canned ProviderReply -- the same contract the real adapters use."""
    calls: Dict[str, Any] = {}

    class _Provider:
        def __init__(self, settings):
            self.name = name
            calls["settings"] = settings

        async def chat(self, system, user, *, model, json_mode=False, history=(),
                       timeout=30.0):
            calls["model"] = model
            calls["user"] = user
            calls["history"] = list(history)
            if ok:
                return ProviderReply(True, self.name, model, text=text)
            return ProviderReply(False, self.name, model, error=error)

    return _Provider, calls


def test_chat_uses_selected_provider_and_configured_model(client, monkeypatch):
    """Selecting a provider must route the real request through that adapter,
    with the configured model, and return the verbatim answer."""
    import sys
    api = sys.modules["jarvis.api.app"]

    fake, calls = _fake_provider("openai", "gpt-4o-mini", ok=True, text="hello there")
    monkeypatch.setattr(api, "select_provider", lambda s: fake(s))
    # A stored key, so the route gets past the credential check.
    monkeypatch.setattr(api.KeychainStorage, "retrieve", staticmethod(lambda pid: "sk-test-123"))
    # A configured model that must reach the wire.
    monkeypatch.setattr(api.get_settings(), "llm_model", "gpt-4o-mini", raising=False)
    monkeypatch.setattr(api.get_settings(), "ai_provider", "openai", raising=False)

    resp = client.post("/ai/chat/send", json={"text": "hi"},
                       headers={"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["response"] == "hello there"
    assert data["provider"] == "openai"
    # The configured model, not a hard-coded one, went out on the wire.
    assert calls.get("model") == "gpt-4o-mini", calls


def test_chat_reports_provider_failure_honestly(client, monkeypatch):
    """A vendor refusal must come back as an honest failure -- never a fake
    success, and never a 500 that hides the vendor's own reason."""
    import sys
    api = sys.modules["jarvis.api.app"]

    fake, _ = _fake_provider("openai", "gpt-4o-mini", ok=False,
                             error="provider returned 401: invalid api key")
    monkeypatch.setattr(api, "select_provider", lambda s: fake(s))
    monkeypatch.setattr(api.KeychainStorage, "retrieve", staticmethod(lambda pid: "sk-bad"))
    monkeypatch.setattr(api.get_settings(), "ai_provider", "openai", raising=False)

    resp = client.post("/ai/chat/send", json={"text": "hi"},
                       headers={"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"})
    assert resp.status_code == 200, "a vendor failure is a normal outcome, not a 500"
    data = resp.json()
    assert data["ok"] is False
    assert data["response"] == ""
    assert "invalid api key" in data["error"]
    assert data["provider"] == "openai"


def test_chat_without_stored_key_fails_safely(client, monkeypatch):
    """No key -> a clear 401, and no provider call is attempted."""
    import sys
    api = sys.modules["jarvis.api.app"]

    fake, calls = _fake_provider("openai", "gpt-4o-mini", ok=True, text="should not be reached")
    monkeypatch.setattr(api, "select_provider", lambda s: fake(s))
    monkeypatch.setattr(api.KeychainStorage, "retrieve", staticmethod(lambda pid: None))
    monkeypatch.setattr(api.get_settings(), "ai_provider", "openai", raising=False)

    resp = client.post("/ai/chat/send", json={"text": "hi"},
                       headers={"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"})
    assert resp.status_code == 401, resp.text
    assert "model" not in calls, "no provider call may happen without a stored key"


def test_chat_uses_the_stored_key_not_config(client, monkeypatch):
    """The key used for the call is the one from the keychain backend, passed to
    the adapter -- and it never appears in the response."""
    import sys
    api = sys.modules["jarvis.api.app"]

    fake, calls = _fake_provider("openai", "gpt-4o-mini", ok=True, text="ok")
    monkeypatch.setattr(api, "select_provider", lambda s: fake(s))
    secret = "sk-a-very-secret-key-1234567890"
    monkeypatch.setattr(api.KeychainStorage, "retrieve", staticmethod(lambda pid: secret))
    monkeypatch.setattr(api.get_settings(), "ai_provider", "openai", raising=False)

    resp = client.post("/ai/chat/send", json={"text": "hi"},
                       headers={"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"})
    assert resp.status_code == 200
    # The stored secret reached the adapter as its api_key...
    assert calls["settings"].llm_api_key == secret
    # ...and is not echoed anywhere in the response.
    assert secret not in resp.text


# ---------------------------------------------------------------------------
# The live configuration bridge -- the defect this work fixes. These use the REAL
# KeychainStorage + select_provider (no mocking of the provider layer), so they prove
# a key saved through the settings API actually reaches the assistant's adapter, and
# that the chosen provider/model is the one resolved. The in-memory keyring from
# conftest keeps them off the real OS keychain.
# ---------------------------------------------------------------------------
def test_saved_key_reaches_the_real_adapter(client):
    """Save a key via the settings API, then resolve the provider through the real
    select_provider and confirm the adapter carries that key -- the whole point of the
    bridge (previously the UI's key never reached the router, which read only the env)."""
    from jarvis.gateway.providers import select_provider
    from jarvis.config import Settings

    secret = "sk-live-bridge-test-1234567890"
    r = client.post("/ai/providers/openai/store", json={"api_key": secret},
                    headers={"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"})
    assert r.status_code == 200, r.text

    # No env key: the key must come from the keychain via the bridge.
    settings = Settings(llm_api_key="", llm_base_url="", ai_provider="openai")
    provider = select_provider(settings)
    assert provider.name == "openai"
    assert provider.api_key == secret, "the saved key did not reach the adapter"
    assert provider.base_url, "the adapter has no endpoint to call"


def test_llm_enabled_only_when_a_key_is_stored(client):
    """`llm_enabled` must reflect an actually-available key: false with none stored,
    true once one is, and false again after it is removed -- never a bare env flag."""
    from jarvis.config import Settings

    s = Settings(llm_api_key="", llm_base_url="", ai_provider="openai")
    assert s.llm_enabled is False

    client.post("/ai/providers/openai/store", json={"api_key": "sk-enabled-test-123456"},
                headers={"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"})
    assert Settings(llm_api_key="", llm_base_url="", ai_provider="openai").llm_enabled is True

    client.delete("/ai/providers/openai/credentials",
                  headers={"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"})
    assert Settings(llm_api_key="", llm_base_url="", ai_provider="openai").llm_enabled is False, \
        "removing the key must stop the provider being usable"


def test_selection_persists_and_refuses_unknown_provider(client):
    """The selection endpoint persists provider/model and refuses a name with no
    adapter, rather than silently storing something the assistant cannot call."""
    r = client.post("/ai/selection", json={"ai_provider": "gemini", "llm_model": "gemini-2.5-flash"},
                    headers={"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"})
    assert r.status_code == 200, r.text
    assert r.json()["effective"]["ai_provider"] == "gemini"

    g = client.get("/ai/selection", headers={"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"}).json()
    assert g["ai_provider"] == "gemini"
    assert g["llm_model"] == "gemini-2.5-flash"

    bad = client.post("/ai/selection", json={"ai_provider": "no-such-vendor"},
                      headers={"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"})
    assert bad.status_code == 400
    # Restore so later tests are not affected by the persisted selection.
    client.post("/ai/selection", json={"ai_provider": "openai"},
                headers={"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"})


def test_disabling_ai_defeats_a_stored_key(client):
    """A stored key must not override an explicit 'off'. When the assistant is disabled,
    `llm_enabled` is False even though a key is present, so requests stay on the rules."""
    from jarvis.config import Settings

    client.post("/ai/providers/openai/store", json={"api_key": "sk-enabled-then-off-123"},
                headers={"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"})
    assert Settings(llm_api_key="", llm_base_url="", ai_provider="openai").llm_enabled is True

    client.post("/ai/selection", json={"ai_enabled": False},
                headers={"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"})
    assert Settings(llm_api_key="", llm_base_url="", ai_provider="openai").llm_enabled is False, \
        "a stored key must not defeat an explicit disable"

    client.post("/ai/selection", json={"ai_enabled": True},
                headers={"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"})
    assert Settings(llm_api_key="", llm_base_url="", ai_provider="openai").llm_enabled is True


# ---------------------------------------------------------------------------
# Optional live integration test -- requires a real key + network. It skips
# itself honestly when either is absent; it is never a substitute for the
# deterministic tests above.
# ---------------------------------------------------------------------------
@pytest.mark.slow
def test_live_openai_if_credentials_present(client, monkeypatch):
    import sys
    api = sys.modules["jarvis.api.app"]
    from jarvis.config import get_settings

    settings = get_settings()
    key = settings.llm_api_key
    # Only attempt when the operator has put a real key in the environment/config.
    if not key or key.startswith(("sk-test", "k", "x")) or len(key) < 20:
        pytest.skip("no real API key configured (live test is optional)")

    monkeypatch.setattr(api, "select_provider",
                        lambda s: __import__("jarvis.gateway.providers", fromlist=["select_provider"]).select_provider(s))
    resp = client.post("/ai/chat/send", json={"text": "Reply with exactly: pong"},
                       headers={"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"}, timeout=60)
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True, data
    assert data["response"].strip()
