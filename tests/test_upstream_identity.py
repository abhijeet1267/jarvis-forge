"""Upstream-identity extraction from 9Router responses.

The gateway exposes upstream information inconsistently: on success the body's
``model`` field names the resolved upstream, and on failure the error message
names a *route id* (``openai-compatible-chat-<uuid>``). These tests pin that the
adapter surfaces what is actually present and never invents an upstream name.
"""

from __future__ import annotations

from jarvis.gateway.providers import NineRouterProvider, OpenAIProvider


def test_success_exposes_resolved_upstream_model():
    """`smooth` routed to `poolside/laguna-s-2.1:free`; the response `model` names it."""
    body = (
        '{"id":"gen-1","object":"chat.completion","model":"poolside/laguna-s-2.1:free",'
        '"provider":"Poolside","choices":[{"message":{"content":"pong"}}]}'
    )
    r = NineRouterProvider(None).parse(body.encode(), "smooth")
    assert r.ok
    assert r.notes.get("upstream_model") == "poolside/laguna-s-2.1:free"
    assert r.notes.get("upstream_provider") == "Poolside"


def test_upstream_identity_known_is_false_when_gateway_hides_it():
    """A plain OpenAI answer has no upstream metadata; identity is not invented."""
    body = '{"choices":[{"message":{"content":"pong"}}]}'
    r = OpenAIProvider(None).parse(body.encode(), "m")
    assert r.ok
    assert r.notes.get("upstream_model", "") == ""
    assert "upstream_provider" not in r.notes


def test_9router_error_extracts_route_id_without_claiming_a_provider():
    body = (
        '{"error":{"message":"[openai-compatible-chat-7f21f9f4-3968-41bd-97ec-7e53b6e08eb3/nope-dead-model]'
        ' [404]: {\\"error\\":{\\"message\\":\\"Model not exist.\\"}}"}}'
    )
    r = NineRouterProvider(None).parse(body.encode(), "ali/nope-dead-model")
    assert not r.ok
    # The route id is surfaced under upstream_route_id, never framed as a vendor name.
    assert r.notes.get("upstream_route_id") == "7f21f9f4-3968-41bd-97ec-7e53b6e08eb3"
    # The nested "Model not exist" is a confirmed dead route, classified as such.
    assert r.notes.get("category") == "route_unavailable"


def test_error_without_route_id_does_not_fabricate_one():
    body = '{"error":{"message":"something went wrong"}}'
    r = NineRouterProvider(None).parse(body.encode(), "m")
    assert not r.ok
    assert r.notes.get("upstream_route_id", "") == ""


def test_redaction_never_leaks_the_key_into_upstream_fields():
    """No upstream field may ever carry the secret, even if an error echoes it."""
    secret = "sk-secret-1234567890"
    body = ('{"error":{"message":"rejected Bearer ' + secret + '"}}').encode()

    class _S:
        ai_provider = "openai"
        llm_base_url = "https://api.openai.com/v1"
        llm_api_key = secret
        llm_model = "m"
        home = None

    r = OpenAIProvider(_S()).parse(body, "m")
    assert not r.ok
    assert secret not in r.error, r.error
    assert secret not in str(r.notes)