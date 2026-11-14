"""Provider failure classification — the category that makes errors diagnosable.

A failed model call must be classifiable without parsing its prose: auth vs
rate-limit vs model-not-found vs bad-request vs server-error vs network vs
timeout vs malformed-response. This guard pins that mapping so a future change
cannot silently fold an invalid key into a generic "provider failed".
"""

from __future__ import annotations

import pytest

from jarvis.gateway.providers import _error_category, ProviderReply, OpenAIProvider


def test_error_category_maps_http_statuses_distinctly():
    assert _error_category(401) == "auth_failed"
    assert _error_category(403) == "auth_failed"
    assert _error_category(404) == "model_not_found"
    assert _error_category(429) == "rate_limited"
    assert _error_category(400) == "bad_request"
    assert _error_category(410) == "upstream_gone"
    assert _error_category(500) == "server_error"
    assert _error_category(503) == "server_error"
    assert _error_category(42) == "upstream_failure"


def test_post_surfaces_a_timeout_category(monkeypatch):
    import jarvis.gateway.providers as providers

    seen = {}

    def fake_http_read(request, *, total, what, gap):
        seen["gap"] = gap
        from jarvis.net import DeadlineExceeded
        raise DeadlineExceeded(5.0, what)

    monkeypatch.setattr(providers, "http_read", fake_http_read)

    class _S:
        ai_provider = "openai"
        llm_base_url = "https://api.openai.com/v1"
        llm_api_key = "k"
        llm_model = "m"
        home = None

    p = OpenAIProvider(_S())
    reply = p.verify_connection(timeout=5.0, model="m")
    assert not reply.ok
    assert reply.notes.get("category") == "timeout", reply.notes


def test_parse_malformed_json_is_classified(monkeypatch):
    import jarvis.gateway.providers as providers

    # Feed a body that is not JSON: it must be classified, not treated as emptiness.
    p = OpenAIProvider(None)
    reply = p.parse(b'{"choices": [', model="m")
    assert not reply.ok
    assert reply.notes.get("category") == "malformed_response", reply.notes


def test_parse_empty_choices_is_classified(monkeypatch):
    p = OpenAIProvider(None)
    reply = p.parse(b'{"choices": []}', model="m")
    assert not reply.ok
    # The existing parse returns "provider returned no choices"; category must exist
    # and be distinguishable from auth/timeout.
    assert reply.notes.get("category") is not None, reply.notes


def test_http_401_becomes_auth_failed_category():
    """An auth refusal is auth_failed, never a generic upstream failure."""
    import jarvis.gateway.providers as providers

    class _S:
        ai_provider = "openai"
        llm_base_url = "https://api.openai.com/v1"
        llm_api_key = "k"
        llm_model = "m"
        home = None

    class _HTTPError(Exception):
        code = 401

        def read(self):
            return b'{"error": {"message": "invalid api key"}}'

    p = OpenAIProvider(_S())
    # Drive _post's HTTPError branch directly.
    import urllib.request
    import urllib.error

    seen = {}

    def fake_read(request, *, total, what, gap):
        raise urllib.error.HTTPError("url", 401, "Unauthorized", {}, None)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(providers, "http_read", fake_read)
    try:
        reply = p.verify_connection(timeout=5.0, model="m")
    finally:
        monkeypatch.undo()

    assert not reply.ok
    assert reply.status == 401
    assert reply.notes.get("category") == "auth_failed", reply.notes

def test_socket_timeout_is_classified_as_timeout_not_network(monkeypatch):
    """A socket inactivity timeout must read as `timeout`, not `network`. This
    regressed when `socket.timeout` fell into the generic `except Exception`
    branch. `timeout` is the category that counts as route evidence, so the
    distinction matters for dead-route detection."""
    import jarvis.gateway.providers as providers
    from jarvis.gateway.providers import OpenAIProvider

    def fake_http_read(request, *, total, what, gap):
        raise TimeoutError("timed out")

    monkeypatch.setattr(providers, "http_read", fake_http_read)

    class _S:
        ai_provider = "openai"
        llm_base_url = "https://api.openai.com/v1"
        llm_api_key = "k"
        llm_model = "m"
        home = None

    p = OpenAIProvider(_S())
    reply = p.verify_connection(timeout=5.0, model="m")
    assert not reply.ok
    assert reply.notes.get("category") == "timeout", reply.notes


def test_oserror_is_timeout_or_network_never_silently_success(monkeypatch):
    """A transport-level OSError is a failure with a category, never a fake 200."""
    import jarvis.gateway.providers as providers
    from jarvis.gateway.providers import OpenAIProvider

    def fake_http_read(request, *, total, what, gap):
        raise OSError("connection reset")

    monkeypatch.setattr(providers, "http_read", fake_http_read)

    class _S:
        ai_provider = "openai"
        llm_base_url = "https://api.openai.com/v1"
        llm_api_key = "k"
        llm_model = "m"
        home = None

    p = OpenAIProvider(_S())
    reply = p.verify_connection(timeout=5.0, model="m")
    assert not reply.ok
    assert reply.notes.get("category") in ("timeout", "network"), reply.notes
