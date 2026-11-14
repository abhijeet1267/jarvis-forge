"""Mock OpenAI-compatible server — the request contract, proven without a paid call.

The server records exactly what a real adapter sends and answers in the OpenAI
chat-completion shape, so the full configuration-to-request-to-response path can
be asserted deterministically. A mock is protocol proof, not vendor proof; the
distinction is stated in the docstring, not left implicit.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest


class _Recorder(BaseHTTPRequestHandler):
    server_version = "mock-openai/1.0"

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        self.server.seen.append({
            "path": self.path,
            "auth": self.headers.get("Authorization", ""),
            "content_type": self.headers.get("Content-Type", ""),
            "body": json.loads(raw.decode() or "{}"),
        })
        body = json.dumps({
            "id": "mock-1",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant",
                                                  "content": "mock answer"},
                         "finish_reason": "stop"}],
            "usage": {"total_tokens": 10},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silent
        pass


class _MockServer:
    def __init__(self):
        self.seen = []
        self.server = HTTPServer(("127.0.0.1", 0), _Recorder)
        self.server.seen = self.seen
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _Settings(base_url, api_key, model):
    class S:
        ai_provider = "openai"
        llm_api_key = api_key
        llm_base_url = base_url
        llm_model = model
        home = None
    return S()


def test_custom_provider_sends_exact_wire_contract():
    """A user-supplied OpenAI-compatible endpoint must receive /chat/completions,
    a Bearer header, the exact model id, and stream:false — with a `/v1` prefix
    preserved exactly once, never doubled."""
    import asyncio

    from jarvis.gateway.providers import OpenAICompatibleProvider

    server = _MockServer()
    try:
        settings = _Settings(server.base_url, "sk-custom-123456", "zcode/gpt-4o")
        provider = OpenAICompatibleProvider(settings)
        reply = asyncio.run(provider.chat("", "hello", model="zcode/gpt-4o"))
        assert reply.ok, reply.error

        seen = server.seen[-1]
        assert seen["path"] == "/v1/chat/completions", seen["path"]
        assert seen["auth"] == "Bearer sk-custom-123456"
        assert seen["body"]["model"] == "zcode/gpt-4o"
        assert seen["body"]["stream"] is False
        assert seen["body"]["messages"][-1]["role"] == "user"
    finally:
        server.stop()


def test_verify_connection_against_mock_completion():
    """verify_connection against a mock proves it exercises the chat path and
    treats a real JSON completion as a pass -- not a GET /models."""
    from jarvis.gateway.providers import OpenAICompatibleProvider

    server = _MockServer()
    try:
        settings = _Settings(server.base_url, "sk-custom-123456", "m/model")
        provider = OpenAICompatibleProvider(settings)
        reply = provider.verify_connection(timeout=5.0, model="m/model")
        assert reply.ok, reply.error
        assert server.seen[-1]["path"] == "/v1/chat/completions"
    finally:
        server.stop()


def test_custom_model_not_required_in_hardcoded_list():
    """A custom model id must not need to appear in any hardcoded list: the server
    is the authority on what model it serves."""
    from jarvis.gateway.providers import OpenAICompatibleProvider

    assert OpenAICompatibleProvider.SUPPORTED_MODELS == []
    assert "does-not-exist-anywhere/model" not in \
        OpenAICompatibleProvider.SUPPORTED_MODELS