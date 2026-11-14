"""Route-health ledger and the /ai/providers/{id}/health endpoint.

These are deterministic (no live gateway): the ledger is an in-memory registry and
the endpoint reads it plus a cheap reachability probe. The tests prove the mapping
from evidence to state, that a GET never issues a completion, and that observations
are keyed to the right provider/model without cross-attribution.
"""

from __future__ import annotations

import pytest

from jarvis.gateway.route_health import (
    RouteHealthRegistry,
    compute_state,
    get_route_health,
    reset_route_health,
)


@pytest.fixture(autouse=True)
def _reset():
    reset_route_health()
    yield
    reset_route_health()


# ---------------------------------------------------------------------------
# State mapping
# ---------------------------------------------------------------------------

def test_no_evidence_is_unknown_when_configured():
    assert compute_state({}, configured=True) == "unknown"


def test_not_configured_beats_everything():
    entry = {"last_success_at": 123, "consecutive_failures": 0}
    assert compute_state(entry, configured=False) == "not_configured"


def test_recent_success_is_healthy():
    entry = {"last_success_at": 123, "last_failure_at": None, "consecutive_failures": 0}
    assert compute_state(entry, configured=True) == "healthy"


def test_one_failure_is_not_unhealthy():
    # A single failure updates health but does not (yet) mark the route degraded.
    entry = {"last_success_at": None, "last_failure_at": 1, "consecutive_failures": 1}
    assert compute_state(entry, configured=True) == "unknown"


def test_repeated_timeouts_become_degraded_then_unhealthy():
    reg = RouteHealthRegistry()
    for i in range(5):
        reg.record("9router", "m", ok=False, source="chat", status=0, category="timeout")
    entry = reg.snapshot("9router", "m")
    assert entry["consecutive_failures"] == 5
    assert compute_state(entry, configured=True) == "unhealthy"

    reg2 = RouteHealthRegistry()
    for i in range(2):
        reg2.record("9router", "m", ok=False, source="chat", status=0, category="timeout")
    assert compute_state(reg2.snapshot("9router", "m"), configured=True) == "degraded"


def test_auth_failure_does_not_poison_route_health():
    reg = RouteHealthRegistry()
    for _ in range(10):
        reg.record("9router", "m", ok=False, source="chat", status=401, category="auth_failed")
    entry = reg.snapshot("9router", "m")
    # Auth errors are not route evidence: the consecutive-failure counter must stay 0.
    assert entry["consecutive_failures"] == 0
    assert entry["last_error_category"] == "auth_failed"


def test_success_clears_failures():
    reg = RouteHealthRegistry()
    reg.record("9router", "m", ok=False, source="chat", category="timeout")
    reg.record("9router", "m", ok=False, source="chat", category="timeout")
    assert reg.snapshot("9router", "m")["consecutive_failures"] == 2
    reg.record("9router", "m", ok=True, source="chat", category="")
    entry = reg.snapshot("9router", "m")
    assert entry["consecutive_failures"] == 0
    assert entry["last_success_at"] is not None


def test_observations_are_keyed_by_provider_and_model():
    reg = RouteHealthRegistry()
    reg.record("9router", "a", ok=False, source="chat", category="timeout")
    reg.record("openai", "b", ok=True, source="chat", category="")
    assert reg.snapshot("9router", "a")["consecutive_failures"] == 1
    assert reg.snapshot("openai", "b")["consecutive_failures"] == 0


def test_registry_is_bounded():
    reg = RouteHealthRegistry()
    for i in range(600):  # MAX_KEYS = 512
        reg.record("p", f"m{i}", ok=True, source="chat")
    # The oldest keys are evicted, not tracked without bound.
    assert reg.snapshot("p", "m0")["last_success_at"] is None
    assert reg.snapshot("p", "m599")["last_success_at"] is not None


# ---------------------------------------------------------------------------
# Endpoint contract
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from jarvis.api.app import app
    return TestClient(app, base_url="http://127.0.0.1:8765")


def test_health_endpoint_returns_unknown_for_untested_configured(client, monkeypatch):
    import sys
    api = sys.modules["jarvis.api.app"]

    monkeypatch.setattr(api.KeychainStorage, "retrieve", staticmethod(lambda pid: "sk-test-123"))
    monkeypatch.setattr(api.get_settings(), "llm_model", "m", raising=False)

    r = client.get("/ai/providers/openai/health",
                   headers={"X-JARVIS-Token": "test-token-do-not-use-in-production", "Host": "127.0.0.1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["configured"] is True
    assert body["route_health"] == "unknown"


def test_health_endpoint_unknown_provider_is_404(client):
    r = client.get("/ai/providers/no-such/health",
                   headers={"X-JARVIS-Token": "test-token-do-not-use-in-production", "Host": "127.0.0.1"})
    assert r.status_code == 404


def test_health_endpoint_requires_auth(client):
    r = client.get("/ai/providers/openai/health", headers={"Host": "127.0.0.1"})
    assert r.status_code in (401, 403)


def test_health_endpoint_reflects_recorded_observation(client, monkeypatch):
    import sys
    api = sys.modules["jarvis.api.app"]
    from jarvis.gateway.route_health import get_route_health

    monkeypatch.setattr(api.KeychainStorage, "retrieve", staticmethod(lambda pid: "sk-test-123"))
    monkeypatch.setattr(api.get_settings(), "llm_model", "m", raising=False)

    get_route_health().record("openai", "m", ok=False, source="chat", status=429, category="rate_limited")
    r = client.get("/ai/providers/openai/health",
                   headers={"X-JARVIS-Token": "test-token-do-not-use-in-production", "Host": "127.0.0.1"})
    body = r.json()
    assert body["last_http_status"] == 429
    assert body["last_error_category"] == "rate_limited"
    assert body["last_check_source"] == "chat"


def test_health_endpoint_get_does_not_issue_a_completion(client, monkeypatch):
    import sys
    api = sys.modules["jarvis.api.app"]
    from jarvis.gateway.providers import OpenAIProvider, ProviderReply

    calls = {"n": 0}

    def fake_verify(self, *, timeout=5.0, model=""):
        calls["n"] += 1
        return ProviderReply(True, "openai", model, text="ok")

    monkeypatch.setattr(OpenAIProvider, "verify_connection", fake_verify)
    monkeypatch.setattr(api.KeychainStorage, "retrieve", staticmethod(lambda pid: "sk-test-123"))
    monkeypatch.setattr(api.get_settings(), "llm_model", "m", raising=False)

    client.get("/ai/providers/openai/health",
               headers={"X-JARVIS-Token": "test-token-do-not-use-in-production", "Host": "127.0.0.1"})
    assert calls["n"] == 0, "a health GET must never run a model completion"