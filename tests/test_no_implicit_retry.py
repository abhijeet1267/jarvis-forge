"""The provider adapter must make exactly one attempt per chat call.

A chat completion is paid and non-idempotent (no idempotency key), so an automatic
retry after an ambiguous timeout could double-bill or emit a second persisted
assistant reply. The provider's `_post` therefore never retries: it returns the
first failure verbatim, and any fallback is the caller's explicit, policy-bound
choice. This guard pins single-attempt semantics.
"""

from __future__ import annotations

import pytest

import jarvis.gateway.providers as providers


def test_post_makes_exactly_one_attempt_on_network_failure(monkeypatch):
    from jarvis.gateway.providers import OpenAIProvider

    calls = {"n": 0}

    def fake_http_read(request, *, total, what, gap):
        calls["n"] += 1
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
    assert calls["n"] == 1, f"a network failure was retried {calls['n']} times"


def test_post_makes_exactly_one_attempt_on_timeout(monkeypatch):
    from jarvis.gateway.providers import OpenAIProvider
    from jarvis.net import DeadlineExceeded

    calls = {"n": 0}

    def fake_http_read(request, *, total, what, gap):
        calls["n"] += 1
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
    assert calls["n"] == 1, "an ambiguous timeout was retried (double-bill risk)"


def test_post_makes_exactly_one_attempt_on_http_error(monkeypatch):
    import urllib.error

    from jarvis.gateway.providers import OpenAIProvider

    calls = {"n": 0}

    def fake_http_read(request, *, total, what, gap):
        calls["n"] += 1
        raise urllib.error.HTTPError("url", 429, "Too Many Requests", {}, None)

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
    assert calls["n"] == 1, "a 429 was retried"
    assert reply.notes.get("category") == "rate_limited"