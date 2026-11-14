"""The manual probe (`POST /ai/providers/{id}/test`) feeds the health ledger.

A probe is an explicit user action: the provider adapter's `_post` records a
``source=test`` observation via ``_record_health`` so the health endpoint can say
whether a status came from a live test or a real chat, and a probe must classify
route failures correctly — a timeout is route evidence, an auth failure is not.

The tests fake the *transport* layer (`http_read`), not `verify_connection`, so the
real `_post` → `_record_health` recording path is what is exercised.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from tests.conftest import TEST_TOKEN
from jarvis.gateway.route_health import get_route_health, reset_route_health


@pytest.fixture(autouse=True)
def _reset_health():
    reset_route_health()
    yield
    reset_route_health()


@pytest.fixture
def client():
    from jarvis.api.app import app
    return TestClient(app, base_url="http://127.0.0.1:8765")


def _stub_http_read(monkeypatch, body: bytes | None, exc: Exception | None = None):
    """Fake the transport so `_post` runs its real success/failure branches."""
    import jarvis.gateway.providers as providers

    def fake(request, *, total, what, gap):
        if exc is not None:
            raise exc
        return body

    monkeypatch.setattr(providers, "http_read", fake)


def _success_body(text="ok"):
    payload = {"choices": [{"message": {"content": text}}]}
    return json.dumps(payload).encode()


def test_successful_probe_records_a_test_observation(client, monkeypatch):
    import sys
    api = sys.modules["jarvis.api.app"]

    monkeypatch.setattr(api.KeychainStorage, "retrieve", staticmethod(lambda pid: "sk-test-123"))
    monkeypatch.setattr(api.get_settings(), "llm_model", "m", raising=False)
    monkeypatch.setattr(api.get_settings(), "ai_provider", "openai", raising=False)
    _stub_http_read(monkeypatch, _success_body())

    r = client.post("/ai/providers/openai/test",
                    headers={"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"})
    assert r.status_code == 200, r.text
    assert r.json()["success"] is True

    entry = get_route_health().snapshot("openai", "m")
    assert entry["last_check_source"] == "test"
    assert entry["last_success_at"] is not None
    assert entry["consecutive_failures"] == 0


def test_timeout_probe_counts_as_route_failure(client, monkeypatch):
    import sys
    api = sys.modules["jarvis.api.app"]

    monkeypatch.setattr(api.KeychainStorage, "retrieve", staticmethod(lambda pid: "sk-test-123"))
    monkeypatch.setattr(api.get_settings(), "llm_model", "m", raising=False)
    monkeypatch.setattr(api.get_settings(), "ai_provider", "openai", raising=False)
    _stub_http_read(monkeypatch, None, exc=TimeoutError("timed out"))

    client.post("/ai/providers/openai/test",
                headers={"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"})

    entry = get_route_health().snapshot("openai", "m")
    assert entry["last_error_category"] == "timeout"
    assert entry["consecutive_failures"] == 1


def test_auth_failure_probe_does_not_count_as_route_failure(client, monkeypatch):
    import sys
    import urllib.error
    api = sys.modules["jarvis.api.app"]

    monkeypatch.setattr(api.KeychainStorage, "retrieve", staticmethod(lambda pid: "sk-test-123"))
    monkeypatch.setattr(api.get_settings(), "llm_model", "m", raising=False)
    monkeypatch.setattr(api.get_settings(), "ai_provider", "openai", raising=False)
    _stub_http_read(
        monkeypatch, b'{"error":{"message":"invalid key"}}',
        exc=urllib.error.HTTPError("url", 401, "Unauthorized", {}, None),
    )

    client.post("/ai/providers/openai/test",
                headers={"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"})

    entry = get_route_health().snapshot("openai", "m")
    assert entry["last_error_category"] == "auth_failed"
    assert entry["consecutive_failures"] == 0, "auth failure must not poison route health"


def test_probe_recovers_after_prior_failures(client, monkeypatch):
    import sys
    api = sys.modules["jarvis.api.app"]

    monkeypatch.setattr(api.KeychainStorage, "retrieve", staticmethod(lambda pid: "sk-test-123"))
    monkeypatch.setattr(api.get_settings(), "llm_model", "m", raising=False)
    monkeypatch.setattr(api.get_settings(), "ai_provider", "openai", raising=False)

    # Two failures first.
    _stub_http_read(monkeypatch, None, exc=TimeoutError("timed out"))
    client.post("/ai/providers/openai/test", headers={"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"})
    client.post("/ai/providers/openai/test", headers={"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"})
    assert get_route_health().snapshot("openai", "m")["consecutive_failures"] == 2

    # Then success recovers it.
    _stub_http_read(monkeypatch, _success_body())
    client.post("/ai/providers/openai/test", headers={"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"})

    entry = get_route_health().snapshot("openai", "m")
    assert entry["consecutive_failures"] == 0
    assert entry["last_success_at"] is not None


def test_body_model_override_does_not_corrupt_saved_config(client, monkeypatch):
    """A one-off `llm_model` in the probe body must not overwrite the provider's saved
    model in the persisted config."""
    import sys
    api = sys.modules["jarvis.api.app"]
    from jarvis.gateway.selection import get_provider_config

    monkeypatch.setattr(api.KeychainStorage, "retrieve", staticmethod(lambda pid: "sk-test-123"))
    monkeypatch.setattr(api.get_settings(), "llm_model", "saved-model", raising=False)
    monkeypatch.setattr(api.get_settings(), "ai_provider", "openai", raising=False)
    _stub_http_read(monkeypatch, _success_body())

    client.post("/ai/providers/openai/test", json={"llm_model": "throwaway-model"},
                headers={"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"})

    cfg = get_provider_config("openai", api.get_settings().home)
    assert cfg.get("model", "") != "throwaway-model", cfg
    assert cfg.get("last_test_model", "") != "throwaway-model", cfg