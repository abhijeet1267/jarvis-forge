"""OpenAI-compatible response parsing — deterministic body coverage.

These exercise the parser directly (no network) across the response shapes the
mission requires: truncated JSON, an unexpected SSE body, empty/missing assistant
content, a provider error envelope, and a valid completion. Together they pin that
no empty or malformed body is ever reported as a successful assistant response.
"""

from __future__ import annotations

from jarvis.gateway.providers import OpenAIProvider


def _parse(payload: bytes) -> "object":
    p = OpenAIProvider(None)
    return p.parse(payload, "m")


def test_valid_completion_parses():
    r = _parse(b'{"choices":[{"message":{"content":"hello"}}]}')
    assert r.ok, r.error
    assert r.text == "hello"


def test_truncated_json_is_not_success():
    r = _parse(b'{"choices":[{"message":{"content":')
    assert not r.ok
    assert r.notes.get("category") == "malformed_response", r.notes


def test_missing_choices_is_not_success():
    r = _parse(b'{"usage": {"total_tokens": 1}}')
    assert not r.ok


def test_missing_content_is_not_success():
    r = _parse(b'{"choices":[{"message":{}}]}')
    assert not r.ok
    assert r.notes.get("category") == "empty_response", r.notes


def test_empty_content_is_not_success():
    r = _parse(b'{"choices":[{"message":{"content":""}}]}')
    assert not r.ok


def test_provider_error_envelope_is_surfaced():
    r = _parse(b'{"error": {"message": "model not found", "type": "invalid_request"}}')
    assert not r.ok
    assert "model not found" in r.error
    # The nested error body names "model not found", so the category is sharpened
    # from a bare "provider_error" to "model_not_found".
    assert r.notes.get("category") == "model_not_found"


def test_unexpected_sse_body_is_not_json_parsed_into_success():
    """If an SSE stream somehow reaches the synchronous parser (a gateway ignored
    `stream:false`), the raw `data: {…}` lines must decode as malformed JSON and be
    rejected, never folded into a silent empty success."""
    body = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]'
    r = _parse(body)
    assert not r.ok, "an SSE body must not be reported as a successful completion"


def test_empty_body_is_not_success():
    r = _parse(b'')
    assert not r.ok
    assert r.notes.get("category") in ("malformed_response", "empty_response"), r.notes