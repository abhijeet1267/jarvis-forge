"""Provider connection-test endpoint contract (the native "Test" button).

Covers the defect that produced a 422 and "Not tested" on the AI Providers screen:
- The native app's Test button POSTs ``/ai/providers/{id}/test`` with **no body**;
  the route used to require a body (``Body(...)`` → 422 "Field required").
  It now tests the *stored* credential instead.
- A "test connection" is one small chat completion, not a ``GET /models`` +
  hardcoded-model-list check that 9Router / custom endpoints can never pass.

All tests are network-free: they monkeypatch ``verify_connection`` on the
provider class. The live 9Router path is exercised separately.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from jarvis.gateway.providers import NineRouterProvider, ProviderReply
from tests.conftest import TEST_TOKEN


@pytest.fixture
def client():
    from jarvis.api.app import app
    return TestClient(app, base_url="http://127.0.0.1:8765")


@pytest.fixture(autouse=True)
def _patch_verify(monkeypatch):
    """Make verify_connection deterministic and record its `model`."""
    captured: dict[str, Any] = {}

    def fake_verify(self, *, timeout: float = 5.0, model: str = ""):
        captured["model"] = model
        captured["timeout"] = timeout
        return ProviderReply(True, self.name, model, text="connection test passed")

    monkeypatch.setattr(NineRouterProvider, "verify_connection", fake_verify)
    return captured


@pytest.fixture
def store_key(monkeypatch):
    import jarvis.security.providers as sp

    def _set(pid: str, key: str | None):
        monkeypatch.setattr(sp.KeychainStorage, "retrieve",
                            staticmethod(lambda _: key), raising=False)
    return _set


def test_test_endpoint_without_body_is_not_a_422(client, store_key):
    """The app's Test button sends no body; the route must not require one."""
    store_key("9router", "sk-test-key-123456")
    resp = client.post("/ai/providers/9router/test",
                       headers={"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is True


def test_test_endpoint_no_stored_key_is_honest_not_422(client, store_key):
    """No stored key -> an actionable message, not a 422/500."""
    store_key("9router", None)
    resp = client.post("/ai/providers/9router/test",
                       headers={"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is False
    assert "No API key is stored" in body["message"]


def test_test_endpoint_custom_provider_requires_base_url(client, store_key, monkeypatch):
    """A custom provider with a stored key but no base URL fails with a base-URL
    message, not a generic network error."""
    from jarvis.api.app import PROVIDERS, get_settings
    from jarvis.gateway.providers import OpenAICompatibleProvider

    monkeypatch.setattr(OpenAICompatibleProvider, "verify_connection",
                        lambda self, *, timeout=5.0, model="":
                        ProviderReply(True, self.name, "verify", text="ok"))
    store_key("openai_compatible", "sk-test-key-123456")

    resp = client.post("/ai/providers/openai_compatible/test",
                       headers={"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"})
    assert resp.status_code == 200
    body = resp.json()
    # No base URL persisted in the selection -> custom provider is not testable.
    assert body["success"] is False
    assert "base url" in body["message"].lower()


def test_test_endpoint_does_not_change_the_selected_provider(client, store_key):
    """Testing provider A must not mutate the production selection."""
    store_key("9router", "sk-test-key-123456")
    before = client.get("/ai/selection", headers={"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"}).json()
    client.post("/ai/providers/9router/test",
                headers={"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"})
    after = client.get("/ai/selection", headers={"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"}).json()
    assert before["ai_provider"] == after["ai_provider"]
    assert before["llm_model"] == after["llm_model"]

def test_keyless_provider_is_usable_without_a_stored_key(client, store_key, monkeypatch):
    """A loopback connector that authenticates nobody must not be reported as
    unconfigured, refused by Test, or gated off in the assistant.

    The chat2api pool binds 127.0.0.1 and runs with auth disabled, so it has no
    secret to store.  Before ``REQUIRES_KEY`` existed, that read as "no API key" on
    the card, a refusal on the Test button, and ``llm_enabled == False`` in the
    router -- a working endpoint that nothing could use.
    """
    from jarvis.api.app import get_settings
    from jarvis.config import _provider_requires_key
    from jarvis.gateway.providers import Chat2APIProvider

    store_key("chat2api", None)
    assert Chat2APIProvider.REQUIRES_KEY is False
    assert _provider_requires_key("chat2api") is False
    # Every other adapter keeps the old contract.
    assert _provider_requires_key("openai") is True

    monkeypatch.setattr(Chat2APIProvider, "verify_connection",
                        lambda self, *, timeout=5.0, model="":
                        ProviderReply(True, self.name, "gpt-4o-mini", text="ok"))

    cards = {c["provider_id"]: c for c in
             client.get("/ai/providers",
                        headers={"X-JARVIS-Token": TEST_TOKEN,
                                 "Host": "127.0.0.1"}).json()}
    assert cards["chat2api"]["configured"] is True, (
        "a keyless connector must be usable without a stored secret")
    # A keyed provider with no secret is still honestly "not configured".
    assert cards["openai"]["configured"] is False

    resp = client.post("/ai/providers/chat2api/test",
                       headers={"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is True, (
        "Test must call a keyless endpoint instead of refusing for a missing key")


def test_keyless_provider_enables_the_router_and_skips_the_401(client, store_key, monkeypatch):
    """The selection path the app actually uses must agree with the card."""
    from jarvis.api.app import get_settings

    store_key("chat2api", None)
    monkeypatch.setattr(get_settings(), "ai_provider", "chat2api", raising=False)
    assert get_settings().llm_enabled is True

    # A keyed provider with no stored secret still refuses, with the original 401.
    monkeypatch.setattr(get_settings(), "ai_provider", "openai", raising=False)
    assert get_settings().llm_enabled is False


def test_an_unknown_route_is_not_labelled_reachable_without_a_probe():
    """`unknown` used to read "Gateway reachable — route status unknown" always.

    That was a claim with no evidence behind it: a provider whose last request
    timed out still showed "Gateway reachable", because the sentence was a
    constant. The label must be built from what was actually observed.
    """
    from jarvis.gateway.route_health import human_state

    # Nothing probed -> say nothing about reachability.
    assert "reachable" not in human_state("unknown").lower()
    assert human_state("unknown") == "No route observations yet"

    # A probe that failed must not be reported as reachable.
    assert human_state("unknown", gateway_reachable=False) == (
        "Gateway unreachable — route not observed yet")

    # Only a probe that succeeded may use the word.
    assert "reachable" in human_state("unknown", gateway_reachable=True).lower()


def test_the_health_route_reports_the_evidence_it_has():
    """The route must pass its probe result into the label, not a constant."""
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "jarvis" / "api" / "app.py"
    text = source.read_text(encoding="utf-8")
    assert "human_state(state, gateway_reachable=gateway_reachable)" in text, (
        "the health route does not pass reachability evidence into the label")
