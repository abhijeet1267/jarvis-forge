"""9Router provider adapter — deterministic parse coverage.

The 9Router gateway served by 9router (127.0.0.1:20128) appends a streaming
sentinel ``data: [DONE]`` after its single JSON completion answer. The OpenAI
adapter's ``json.loads`` chokes on that trailing text and reports
"unreadable response: Extra data", so every 9Router completion failed. These
tests pin the fix without any network.
"""

from __future__ import annotations

from jarvis.gateway.providers import NineRouterProvider, ProviderReply


def _reply(payload: str) -> "object":
    p = NineRouterProvider(None)
    return p.parse(payload.encode("utf-8"), "smooth")


def test_9router_strips_the_trailing_done_sentinel():
    """A non-streamed 9Router reply is one JSON object + `data: [DONE]`."""
    body = (
        '{"id":"c1","object":"chat.completion","choices":['
        '{"index":0,"message":{"role":"assistant","content":"Hello there friend"},'
        '"finish_reason":"stop"}],"usage":{"total_tokens":57}}\n'
        "data: [DONE]\n"
    )
    reply = _reply(body)
    assert reply.ok, reply.error
    assert reply.text == "Hello there friend"


def test_9router_still_parses_a_plain_json_body():
    """Without the trailing sentinel, parsing behaves like the OpenAI adapter."""
    body = (
        '{"id":"c1","object":"chat.completion","choices":['
        '{"index":0,"message":{"role":"assistant","content":"plain"},'
        '"finish_reason":"stop"}]}'
    )
    reply = _reply(body)
    assert reply.ok, reply.error
    assert reply.text == "plain"


def test_9router_reports_an_error_body_honestly():
    """An error payload is still surfaced as a failure, not mangled."""
    body = '{"error":{"message":"no credits","type":"auth"}}\ndata: [DONE]\n'
    reply = _reply(body)
    assert not reply.ok
    assert reply.error


def test_openai_shape_chat_request_is_non_streamed():
    """The synchronous adapter must tell the server `stream: false`, or an
    SSE-by-default gateway (9Router, OpenRouter) returns `data: {chunk}` lines
    that json.loads cannot decode — which was the "Not tested" / empty-answer
    failure at the provider card."""
    from jarvis.gateway.providers import OpenAIProvider

    p = OpenAIProvider(None)
    _, payload = p._chat_request("", "hi", "gpt-4o-mini", False)
    assert payload.get("stream") is False, payload


def test_verify_connection_uses_a_minimal_chat_completion_path():
    """verify_connection must exercise the real chat path (a small completion),
    not GET /models — many compatible endpoints don't expose a model list. Tested
    by behaviour: it calls `_chat_request` (a POST /chat/completions), never a
    GET /models."""
    from jarvis.gateway.providers import OpenAIProvider

    class _Probe(OpenAIProvider):
        def _chat_request(self, system, user, model, json_mode, history=()):
            self._probe = (system, user, model)
            return "http://example.test/v1/chat/completions", {"model": model, "messages": [], "stream": False}

        def _post(self, url, payload, *, timeout, model, source="chat"):
            assert source == "test", "verify_connection must tag its observation as a test"
            return ProviderReply(True, self.name, model, text="OK")

    p = _Probe(None)
    reply = p.verify_connection(timeout=1.0, model="m")
    assert reply.ok
    assert p._probe == ("", "Reply with exactly: VANTAGE connection test successful", "m"), p._probe