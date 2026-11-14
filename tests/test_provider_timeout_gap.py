"""The model-call socket gap must not be shorter than the call's own budget.

A combo router (9Router ``smooth``) sends one call to a random upstream; many are
*reasoning* models that think silently for seconds before the first token. The
default socket-inactivity gap in ``jarvis.net.http_read`` is 5s, so a silent
reasoning step killed the call ("call failed: timed out") even though the chat
turn's 20s total budget would happily have waited. The provider's chat path must
therefore pass ``gap == total`` so only the call's own deadline can end it.
"""

from __future__ import annotations


def test_provider_chat_post_sets_gap_equal_to_its_budget(monkeypatch):
    from jarvis.gateway.providers import OpenAIProvider

    calls = {}
    real = None
    import jarvis.gateway.providers as providers

    real = providers.http_read

    def fake_http_read(request, *, total, what, gap):
        calls["gap"] = gap
        calls["total"] = total
        # Hand back a valid completion so parse() has something to decode.
        return (b'{"id":"c1","object":"chat.completion",'
                b'"choices":[{"index":0,"message":{"role":"assistant",'
                b'"content":"ok"},"finish_reason":"stop"}]}')

    monkeypatch.setattr(providers, "http_read", fake_http_read)

    class _S:
        ai_provider = "openai"
        llm_base_url = "https://api.openai.com/v1"
        llm_api_key = "k"
        llm_model = "m"
        home = None

    p = OpenAIProvider(_S())
    p.verify_connection(timeout=20.0, model="m")

    assert calls["gap"] == calls["total"] == 20.0, calls


def test_provider_chat_post_still_bounds_by_the_total(monkeypatch):
    """The total budget remains the thing that ends a stalled call; a larger gap does not
    turn the timeout into an unbounded wait, because http_read caps at `total`."""
    from jarvis.gateway.providers import OpenAIProvider, ProviderReply
    import jarvis.gateway.providers as providers

    seen = {}

    def fake_http_read(request, *, total, what, gap):
        seen["total"] = total
        seen["gap"] = gap
        raise TimeoutError("the model endpoint kept the connection open")

    monkeypatch.setattr(providers, "http_read", fake_http_read)

    class _S:
        ai_provider = "openai"
        llm_base_url = "https://api.openai.com/v1"
        llm_api_key = "k"
        llm_model = "m"
        home = None

    p = OpenAIProvider(_S())
    reply = p.verify_connection(timeout=20.0, model="m")

    # A socket.timeout surfaces as a failure, never a fake success, and it carries the
    # whole-call budget it was given.
    assert not reply.ok
    assert seen["gap"] == seen["total"] == 20.0