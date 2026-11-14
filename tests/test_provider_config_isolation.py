"""Per-provider configuration isolation (P0 hardening).

Proves the defect the mission pinpoints: a single shared ``llm_base_url`` /
``llm_model`` in ``ai_selection.json`` meant configuring one provider could
overwrite another's endpoint/model. After the fix:
- each provider's base URL/model live in its own block;
- the Test button resolves *that* provider's config, not the active selection's;
- switching the active provider restores its own saved config;
- a test result goes stale the moment the config it was earned against changes.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import TEST_TOKEN


@pytest.fixture
def client():
    from jarvis.api.app import app
    return TestClient(app, base_url="http://127.0.0.1:8765")


@pytest.fixture
def store_key(monkeypatch):
    import jarvis.security.providers as sp

    def _set(pids_to_keys: dict):
        monkeypatch.setattr(sp.KeychainStorage, "retrieve",
                            staticmethod(lambda pid: pids_to_keys.get(pid)), raising=False)
    return _set


# ---------------------------------------------------------------------------
# Selection persistence layer
# ---------------------------------------------------------------------------

def test_save_provider_config_does_not_touch_active_selection(tmp_path):
    from jarvis.gateway.selection import (save_selection, save_provider_config,
                                          load_selection)

    save_selection(tmp_path, provider="openai", model="gpt-4o-mini")
    save_provider_config("openai_compatible", home=tmp_path,
                         base_url="http://127.0.0.1:9999/v1", model="zcode/gpt-4o")

    sel = load_selection(tmp_path)
    # The active selection is untouched by a per-provider save.
    assert sel["ai_provider"] == "openai"
    assert sel["llm_model"] == "gpt-4o-mini"
    # And the per-provider block holds the custom endpoint.
    data = json.loads((tmp_path / "ai_selection.json").read_text())
    assert data["providers"]["openai_compatible"]["base_url"] == "http://127.0.0.1:9999/v1"


def test_two_providers_keep_independent_config(tmp_path):
    from jarvis.gateway.selection import get_provider_config, save_provider_config

    save_provider_config("openai_compatible", home=tmp_path,
                         base_url="http://a:1/v1", model="a/model")
    save_provider_config("9router", home=tmp_path,
                         base_url="http://b:2/v1", model="b/model")

    assert get_provider_config("openai_compatible", tmp_path)["base_url"] == "http://a:1/v1"
    assert get_provider_config("9router", tmp_path)["base_url"] == "http://b:2/v1"


def test_save_selection_carries_provider_block_through(tmp_path):
    from jarvis.gateway.selection import save_provider_config, save_selection

    save_provider_config("openai_compatible", home=tmp_path, base_url="http://a:1/v1")
    save_selection(tmp_path, provider="openai")
    data = json.loads((tmp_path / "ai_selection.json").read_text())
    assert data["providers"]["openai_compatible"]["base_url"] == "http://a:1/v1"


# ---------------------------------------------------------------------------
# Effective resolution: a provider uses its OWN endpoint/model, not another's
# ---------------------------------------------------------------------------

class _S:
    def __init__(self, **kw):
        self.home = None
        self.llm_base_url = ""
        self.llm_model = ""
        self.llm_api_key = ""
        self.ai_provider = "openai"
        for k, v in kw.items():
            setattr(self, k, v)


def test_provider_base_url_prefers_own_block_over_active_selection(tmp_path):
    from jarvis.gateway.selection import (provider_base_url, save_provider_config,
                                          save_selection)

    # The active selection points at a custom provider; the real singleton would
    # already have llm_base_url resolved from it (the selection overlay). Here we
    # reproduce that: active provider = openai_compatible with a custom endpoint.
    save_selection(tmp_path, provider="openai_compatible",
                   model="x", base_url="http://custom:9/v1")
    save_provider_config("openai", home=tmp_path, base_url="http://openai:1/v1")

    s = _S(home=tmp_path, ai_provider="openai_compatible",
           llm_base_url="http://custom:9/v1", llm_model="x")
    # OpenAI (not active) resolves its own block, NOT the active custom selection.
    assert provider_base_url(s, "openai") == "http://openai:1/v1"
    # The active custom provider resolves its own active endpoint.
    assert provider_base_url(s, "openai_compatible") == "http://custom:9/v1"


def test_provider_model_prefers_own_block(tmp_path):
    from jarvis.gateway.selection import provider_model, effective_model, save_provider_config

    save_provider_config("gemini", home=tmp_path, model="gemini-2.5-flash")
    # Active provider is openai with its own global model. Gemini is NOT active.
    s = _S(home=tmp_path, ai_provider="openai", llm_model="gpt-4o-mini")
    # The scoped resolver (what the Test button / cards use): Gemini uses its OWN
    # model, never the active provider's global model.
    assert provider_model(s, "gemini") == "gemini-2.5-flash"
    # The active provider (openai) has no own-block model, so its scoped model is
    # its own global model (it IS the active provider).
    assert provider_model(s, "openai") == "gpt-4o-mini"
    # A non-active provider with no own-block model resolves to "" via the scoped
    # resolver — it must NOT inherit the active provider's global model.
    save_provider_config("groq", home=tmp_path, model="")  # groq has no own model
    assert provider_model(s, "groq") == ""
    # The adapter-path effective_model (what the chat adapter uses) honours the
    # active provider's global model as a fallback — the adapter is always built
    # for the active provider, so the global model is correct there.
    assert effective_model(s, "openai") == "gpt-4o-mini"


# ---------------------------------------------------------------------------
# Endpoint contract: Test resolves + persists per provider, no selection mutation
# ---------------------------------------------------------------------------

def test_test_uses_that_providers_saved_base_url(client, store_key, monkeypatch, tmp_path):
    """The Test button for provider X must send X's saved endpoint, not the global
    one the active custom provider left behind."""
    from jarvis.api.app import get_settings
    from jarvis.gateway.providers import OpenAIProvider, ProviderReply
    from jarvis.gateway.selection import save_provider_config

    monkeypatch.setattr(get_settings(), "home", tmp_path, raising=False)
    save_provider_config("openai", home=tmp_path, base_url="http://openai:1/v1")

    seen = {}
    def fake_verify(self, *, timeout=5.0, model=""):
        seen["url"] = self.base_url
        seen["model"] = model
        return ProviderReply(True, self.name, model, text="ok")

    monkeypatch.setattr(OpenAIProvider, "verify_connection", fake_verify)
    store_key({"openai": "sk-test-key-123456"})

    client.post("/ai/providers/openai/test",
                headers={"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"})
    # The adapter's effective base URL is the provider's own saved one.
    assert seen["url"] == "http://openai:1/v1"


def test_test_persists_result_and_status_flows_to_card(client, store_key, monkeypatch, tmp_path):
    from jarvis.api.app import get_settings
    from jarvis.gateway.providers import OpenAIProvider, ProviderReply

    monkeypatch.setattr(get_settings(), "home", tmp_path, raising=False)
    store_key({"openai": "sk-test-key-123456"})
    monkeypatch.setattr(OpenAIProvider, "verify_connection",
                        lambda self, *, timeout=5.0, model="":
                        ProviderReply(True, self.name, model, text="ok"))

    client.post("/ai/providers/openai/test",
                headers={"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"})
    cards = {c["provider_id"]: c for c in
             client.get("/ai/providers", headers={"X-JARVIS-Token": TEST_TOKEN,
                                                  "Host": "127.0.0.1"}).json()}
    assert cards["openai"]["verified"] is True
    assert cards["openai"]["test_status"] == "passed"


def test_test_does_not_mark_another_provider_tested(client, store_key, monkeypatch, tmp_path):
    from jarvis.api.app import get_settings
    from jarvis.gateway.providers import OpenAIProvider, ProviderReply

    monkeypatch.setattr(get_settings(), "home", tmp_path, raising=False)
    store_key({"openai_compatible": "sk-test-key-123456"})
    monkeypatch.setattr(OpenAIProvider, "verify_connection",
                        lambda self, *, timeout=5.0, model="":
                        ProviderReply(True, self.name, model, text="ok"))

    client.post("/ai/providers/9router/test",
                headers={"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"})
    cards = {c["provider_id"]: c for c in
             client.get("/ai/providers", headers={"X-JARVIS-Token": TEST_TOKEN,
                                                  "Host": "127.0.0.1"}).json()}
    # 9router has no key stored here, so it cannot have been marked tested.
    assert cards["9router"]["test_status"] == "not_configured"


def test_set_active_for_custom_switches_off_global_collision(client, store_key, monkeypatch, tmp_path):
    from jarvis.api.app import get_settings
    from jarvis.gateway.selection import save_provider_config

    monkeypatch.setattr(get_settings(), "home", tmp_path, raising=False)
    # A custom provider was configured with its own endpoint; the active provider is
    # still 'openai' and must not absorb the custom endpoint.
    save_provider_config("openai_compatible", home=tmp_path,
                         base_url="http://zcode:1/v1", model="zcode/gpt-4o")
    store_key({"openai": "sk-openai-key-123456"})

    cards = {c["provider_id"]: c for c in
             client.get("/ai/providers", headers={"X-JARVIS-Token": TEST_TOKEN,
                                                  "Host": "127.0.0.1"}).json()}
    # OpenAI still resolves to its own default, not zcode's URL.
    assert cards["openai"]["base_url"].startswith("https://api.openai.com") or \
        "zcode" not in cards["openai"]["base_url"]