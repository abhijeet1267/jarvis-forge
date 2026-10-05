"""Phase 60: a second vendor, without a second authority.

Measured before writing anything. `jarvis/gateway/router.py` was 149 lines that spoke exactly one
wire format -- `{llm_base_url}/chat/completions` with a `Bearer` header, model names from config,
`provider="openai-compatible"` hard-coded in the choice it reports. Memory's embedder, the vision
provider and the voice transcriber each build their own OpenAI-shaped URL from the same two
settings. So a Gemini key on this machine would have configured successfully, lit up the
`llm_enabled` badge on `/health`, and then been called at the wrong address in the wrong shape by
every one of those four paths.

That is the thing this module pins down: the *contract* is provider-neutral, the *authority* is not
a provider's. No model, from any vendor, plans or executes here -- the pipeline's gate stays the
only door, exactly as Phase 42 left it.

Every call in these tests goes to a local fake endpoint that records what it was asked. That is
protocol proof, not a vendor proof: there is no API key on this machine (`/health` answers
`llm_enabled: false`, and `~/.jarvis-forge/llm.env` does not exist), so nothing here demonstrates
that a real Gemini or OpenAI endpoint accepts what we send. The phase report says so in those words.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from jarvis.gateway.providers import (GeminiProvider, OpenAIProvider, ProviderUnknown,
                                      select_provider)

KNOWN = {"openai", "gemini"}


# ---------------------------------------------------------------------------
# a server that records the request, so the wire format is a fact and not an opinion
# ---------------------------------------------------------------------------


class _Recorder(BaseHTTPRequestHandler):
    """Answers in whichever vendor shape the path implies, and remembers what arrived."""

    server_version = "fake-vendor/1.0"

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        self.server.seen.append({
            "method": self.command,
            "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": json.loads(raw.decode() or "{}"),
        })
        payload, status = self.server.answer_for(self.path, self.server.seen[-1]["body"])
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silent
        pass


class _Vendor:
    """A loopback stand-in for a model vendor."""

    def __init__(self, answers=None):
        self.seen = []
        self.answers = answers or {}
        self.server = HTTPServer(("127.0.0.1", 0), _Recorder)
        self.server.seen = self.seen
        self.server.answer_for = self.answer_for
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def answer_for(self, path, body):
        for needle, reply in self.answers.items():
            if needle in path:
                return reply, 200
        if "generateContent" in path:
            return {"candidates": [{"content": {"parts": [{"text": "gemini answered"}]}}]}, 200
        if path.endswith("/chat/completions"):
            return {"choices": [{"message": {"content": "openai answered"}}]}, 200
        return {}, 404

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    @property
    def last(self):
        return self.seen[-1]


@pytest.fixture()
def client():
    """The served app, addressed at loopback so the host gate is not what answers."""
    import importlib

    from fastapi.testclient import TestClient

    from tests.conftest import AUTH_HEADERS

    module = importlib.import_module("jarvis.api.app")
    return TestClient(module.app, base_url="http://127.0.0.1:8765", headers=AUTH_HEADERS)


def _health_settings(client):
    """The settings instance `/health` will read, whichever core the app is holding."""
    import importlib

    api = importlib.import_module("jarvis.api.app")
    return api.get_core().settings

@pytest.fixture()
def vendor():
    server = _Vendor()
    yield server
    server.stop()


class _Settings:
    """Only the fields a provider reads, so a missing one is a decision, not an accident."""

    def __init__(self, base_url, *, provider="openai", model="configured-model",
                 key="a-key-that-must-never-be-echoed", embed_model="configured-embed",
                 stt_model="configured-stt"):
        self.ai_provider = provider
        self.llm_base_url = base_url
        self.llm_api_key = key
        self.llm_model = model
        self.llm_fast_model = model
        self.llm_reasoning_model = model
        self.llm_embed_model = embed_model
        self.stt_model = stt_model
        self.llm_enabled = bool(base_url and key)


# ---------------------------------------------------------------------------
# 1. selection, and what an unknown name does
# ---------------------------------------------------------------------------


def test_the_default_provider_is_the_shape_that_already_worked():
    """Nothing here changes what an existing OpenAI-shaped install does on the wire."""
    provider = select_provider(_Settings("http://127.0.0.1:1/v1"))
    assert provider.name == "openai"


def test_a_second_vendor_is_selected_by_configuration_not_by_code():
    """§10/§11 of the spec: the architecture must not depend on one vendor."""
    provider = select_provider(_Settings("http://127.0.0.1:1/v1", provider="gemini"))
    assert provider.name == "gemini"
    assert isinstance(provider, GeminiProvider)


def test_an_unknown_provider_name_is_refused_rather_than_defaulted():
    """A typo in a config that silently means "we used the other vendor anyway" is a lie
    the status page would then repeat back as the vendor it did not use."""
    with pytest.raises(ProviderUnknown) as refused:
        select_provider(_Settings("http://127.0.0.1:1/v1", provider="claude"))
    assert "claude" in str(refused.value)
    for name in sorted(KNOWN):
        assert name in str(refused.value), f"the refusal must say what is available: {name}"


# ---------------------------------------------------------------------------
# 2. the two wire shapes, read off the socket
# ---------------------------------------------------------------------------


def test_the_openai_shape_is_the_one_that_was_already_on_the_wire(vendor):
    provider = OpenAIProvider(_Settings(vendor.base_url))
    reply = asyncio.run(provider.chat("system words", "user words", model="m1"))
    assert reply.ok, reply.error
    assert reply.text == "openai answered"
    seen = vendor.last
    assert seen["path"] == "/v1/chat/completions"
    assert seen["headers"]["authorization"] == "Bearer a-key-that-must-never-be-echoed"
    assert seen["body"]["model"] == "m1"
    assert [m["role"] for m in seen["body"]["messages"]] == ["system", "user"]


def test_the_gemini_shape_is_its_own_and_not_openai_with_renamed_fields(vendor):
    provider = GeminiProvider(_Settings(vendor.base_url))
    reply = asyncio.run(provider.chat("system words", "user words", model="gemini-2.5-flash"))
    assert reply.ok, reply.error
    assert reply.text == "gemini answered"
    seen = vendor.last
    assert seen["path"] == "/v1beta/models/gemini-2.5-flash:generateContent"
    assert seen["headers"]["x-goog-api-key"] == "a-key-that-must-never-be-echoed"
    assert "authorization" not in seen["headers"], "a Bearer header is not what this vendor takes"
    assert seen["body"]["systemInstruction"]["parts"][0]["text"] == "system words"
    assert [p["text"] for p in seen["body"]["contents"][0]["parts"]] == ["user words"]


def test_the_model_name_comes_from_configuration_on_both_sides(vendor):
    """§10's rule: do not hard-code model names. The check is that the wire moves when the
    setting does, for each vendor, rather than that some string appears in a source file."""
    for cls, expect in ((OpenAIProvider, lambda s: s["body"]["model"] == "from-config"),
                        (GeminiProvider, lambda s: "/models/from-config:generateContent" in s["path"])):
        vendor.seen.clear()
        asyncio.run(cls(_Settings(vendor.base_url)).chat("s", "u", model="from-config"))
        assert expect(vendor.last), (cls.__name__, vendor.last["path"], vendor.last["body"])


def test_json_mode_is_asked_for_in_the_vendors_own_language(vendor):
    """One flag, two mechanisms: `response_format` here, `responseMimeType` there. A caller
    must not have to know which, and the test exists so the two cannot drift apart."""
    asyncio.run(OpenAIProvider(_Settings(vendor.base_url)).chat("s", "u", model="m",
                                                                json_mode=True))
    assert vendor.last["body"]["response_format"] == {"type": "json_object"}
    asyncio.run(GeminiProvider(_Settings(vendor.base_url)).chat("s", "u", model="m",
                                                                json_mode=True))
    assert vendor.last["body"]["generationConfig"]["responseMimeType"] == "application/json"


# ---------------------------------------------------------------------------
# 3. failure has a shape, and it is never an empty success
# ---------------------------------------------------------------------------


def test_a_vendor_refusal_is_reported_as_the_vendors_own_words(vendor):
    provider = GeminiProvider(_Settings(vendor.base_url))
    vendor.answers["generateContent"] = {"error": {"message": "model not found", "status": 404}}
    reply = asyncio.run(provider.chat("s", "u", model="nonexistent"))
    assert not reply.ok
    assert "model not found" in reply.error, "the vendor said why; the user gets told why"
    assert reply.text == ""


def test_a_timeout_is_a_timeout_with_its_number_in_it():
    """A dribbling server proves the same thing here as in Phase 34: an inactivity timeout is
    not a deadline, and an unanswered question must not arrive as a blank answer."""

    class _Slow:
        def __init__(self):
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.bind(("127.0.0.1", 0))
            self.sock.listen(4)
            self.port = self.sock.getsockname()[1]
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

        def _run(self):
            while True:
                try:
                    conn, _ = self.sock.accept()
                except OSError:
                    return
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 99999999\r\n\r\n")
                while True:
                    try:
                        conn.sendall(b" ")
                    except OSError:
                        break

        def stop(self):
            self.sock.close()

    slow = _Slow()
    try:
        provider = OpenAIProvider(_Settings(f"http://127.0.0.1:{slow.port}/v1"))
        reply = asyncio.run(provider.chat("s", "u", model="m", timeout=1.0))
        assert not reply.ok
        assert "timeout" in reply.error.lower(), reply.error
    finally:
        slow.stop()


def test_a_capability_a_provider_does_not_have_says_so_instead_of_going_silent(vendor):
    """Gemini's REST surface here has no transcription shape, and neither provider is wired to
    the voice path yet. Returning `""` would be an empty transcript -- the offline-as-empty bug
    this project has already fixed twice, for mail and for calendar, in a new place. A reply with
    `ok: false` could still be ignored by a careless caller, so this raises instead.
    """
    from jarvis.gateway.providers import ProviderCapabilityMissing

    for provider in (GeminiProvider(_Settings(vendor.base_url)),
                     OpenAIProvider(_Settings(vendor.base_url))):
        with pytest.raises(ProviderCapabilityMissing) as refused:
            provider.transcribe(b"fake audio bytes")
        assert "transcription" in str(refused.value).lower()


def test_the_key_never_comes_back_in_what_a_caller_is_told(vendor):
    """The whole gateway is one place a secret is put on the wire; its report of itself is the
    place it could come back. Nothing in a reply or an error may repeat it."""
    for provider in (OpenAIProvider, GeminiProvider):
        reply = asyncio.run(provider(_Settings(vendor.base_url)).chat("s", "u", model="m"))
        assert "a-key-that-must-never-be-echoed" not in json.dumps(reply.__dict__, default=str)
        assert reply.provider == provider(_Settings(vendor.base_url)).name


# ---------------------------------------------------------------------------
# 4. embeddings and the router, which is where the badge and the truth must agree
# ---------------------------------------------------------------------------


def test_embeddings_go_to_the_vendors_own_endpoint(vendor):
    asyncio.run(OpenAIProvider(_Settings(vendor.base_url)).embed(["one text"], model="e1"))
    assert vendor.last["path"] == "/v1/embeddings"
    assert vendor.last["body"]["model"] == "e1"
    vendor.seen.clear()
    asyncio.run(GeminiProvider(_Settings(vendor.base_url)).embed(["one text"], model="e2"))
    assert vendor.last["path"] == "/v1beta/models/e2:embedContent"


def test_the_router_asks_the_provider_the_configuration_names(vendor, monkeypatch):
    """`/health` prints a badge. If the badge says one vendor and the router calls another,
    the status page is the thing that is wrong, and nobody would look there for a bug."""
    from jarvis.config import get_settings
    from jarvis.gateway.router import ModelRouter

    settings = get_settings()
    monkeypatch.setattr(settings, "llm_base_url", vendor.base_url, raising=False)
    monkeypatch.setattr(settings, "llm_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "ai_provider", "gemini", raising=False)
    router = ModelRouter(settings=settings)
    answer = asyncio.run(router.complete("sys", "usr", task_type="conversation", timeout=5.0))
    assert answer == "gemini answered", answer
    assert vendor.last["path"].endswith(":generateContent")
    assert router.route("conversation").provider == "gemini"


def test_the_router_still_returns_none_when_the_vendor_is_out(vendor, monkeypatch):
    """The contract the orchestrator was built against: no answer is None, and the caller says
    so out loud. A provider layer that raises instead would take the pipeline down at 2 a.m.
    because a vendor had a bad day."""
    from jarvis.config import get_settings
    from jarvis.gateway.router import ModelRouter

    settings = get_settings()
    monkeypatch.setattr(settings, "llm_base_url", "http://127.0.0.1:1/v1", raising=False)
    monkeypatch.setattr(settings, "llm_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "ai_provider", "openai", raising=False)
    assert asyncio.run(ModelRouter(settings=settings).complete("s", "u")) is None


# ---------------------------------------------------------------------------
# 5. authority did not move
# ---------------------------------------------------------------------------


def test_a_model_answer_from_a_second_vendor_still_emits_no_action(core, vendor, monkeypatch):
    """The point of the phase, and the reason a second adapter is allowed at all.

    Phase 42 gave the model the right to speak and nowhere else. Whatever vendor answers, the
    reply still has to say the rule router did not recognise the sentence, the model has to be
    named, and no action may appear.
    """
    from jarvis.config import get_settings
    from jarvis.core.models import CommandRequest
    from jarvis.gateway.router import ModelRouter

    settings = get_settings()
    monkeypatch.setattr(settings, "llm_base_url", vendor.base_url, raising=False)
    monkeypatch.setattr(settings, "llm_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "ai_provider", "gemini", raising=False)
    monkeypatch.setattr(settings, "llm_model", "gemini-2.5-flash", raising=False)
    monkeypatch.setattr(core, "settings", settings, raising=False)
    monkeypatch.setattr(core, "router", ModelRouter(settings=settings))

    response = asyncio.run(core.handle(CommandRequest(
        text="wobbleth-and-glintar please", source="phase60")))

    assert vendor.seen, "the configured vendor was never asked"
    assert vendor.last["path"].endswith(":generateContent"), vendor.last["path"]
    assert "gemini" in response.reply.lower(), response.reply
    assert "gemini" in (response.model_used or "").lower(), response.model_used
    assert not response.actions, [a.tool for a in response.actions]


def test_nothing_claims_a_realtime_session_exists():
    """§10/§11 both ask for `realtime_session`. It is not built: it needs a vendor websocket,
    a key, and a phone that can hold it. This guard exists so that the day a method by that
    name appears, it appears with a proof beside it and not before."""
    from jarvis.gateway import providers as mod

    for name in ("OpenAIProvider", "GeminiProvider"):
        cls = getattr(mod, name)
        assert not hasattr(cls, "realtime_session"), f"{name} claims a realtime session"
    source = open(mod.__file__, encoding="utf-8").read()
    assert "realtime" not in source.lower(), "the word must not appear without the proof"


def test_the_memory_write_path_asks_the_vendor_the_settings_name(vendor, monkeypatch):
    """The path Phase 42 measured as the one a key actually uses.

    Two of the two outbound calls a configured key made were `/v1/embeddings`. So this is where a
    second vendor matters most, and where a wrong URL would mean a paid-for key silently doing
    nothing for the one job it was bought for.
    """
    import jarvis.memory.embeddings as embeddings
    from jarvis.config import get_settings

    vendor.answers[":embedContent"] = {"embedding": {"values": [0.5, 0.25]}}
    settings = get_settings().model_copy(update={
        "ai_provider": "gemini", "llm_base_url": vendor.base_url, "llm_api_key": "k",
        "llm_embed_model": "gemini-embedding"})
    vector = embeddings.RemoteEmbedder(settings=settings).embed("the heater coil arrived")
    assert vector == [0.5, 0.25], vector
    assert vendor.last["path"] == "/v1beta/models/gemini-embedding:embedContent"
    assert vendor.last["headers"]["x-goog-api-key"] == "k"


def test_the_memory_write_path_still_works_for_the_vendor_that_already_worked(vendor):
    """The regression that matters for an existing install: same URL, same header, same bytes.

    Adding a second vendor is only safe if the first one is untouched, and "untouched" here is a
    wire claim, so it is checked on the wire.
    """
    import jarvis.memory.embeddings as embeddings
    from jarvis.config import get_settings

    vendor.answers["/embeddings"] = {"data": [{"embedding": [1.0, 2.0, 3.0]}]}
    settings = get_settings().model_copy(update={
        "ai_provider": "openai", "llm_base_url": vendor.base_url, "llm_api_key": "k",
        "llm_embed_model": "text-embedding-3-small"})
    assert embeddings.RemoteEmbedder(settings=settings).embed("anything") == [1.0, 2.0, 3.0]
    assert vendor.last["path"] == "/v1/embeddings"
    assert vendor.last["headers"]["authorization"] == "Bearer k"
    assert vendor.last["body"] == {"model": "text-embedding-3-small", "input": "anything"}


def test_health_names_the_vendor_it_will_call(client, vendor, monkeypatch):
    """`llm_enabled: true` was the whole story once, and the story was wrong: it said a key
    existed, not which door it would be used at."""
    # Patched on the settings object the running core holds, not on a fresh `get_settings()`
    # call: in a full-suite run those can be different instances, and a test that patches the
    # wrong one passes alone and lies in company.
    settings = _health_settings(client)
    monkeypatch.setattr(settings, "llm_base_url", vendor.base_url, raising=False)
    monkeypatch.setattr(settings, "llm_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "ai_provider", "gemini", raising=False)
    body = client.get("/health").json()
    assert body["llm_enabled"] is True
    assert body["ai_provider"] == "gemini"
    assert body["provider_paths"]["embed"] == "gemini"
    assert "openai-shaped" in body["provider_paths"]["stt"], \
        "the boundary has to be visible, not implied"


def test_an_unusable_provider_name_is_visible_on_the_status_page(client, monkeypatch):
    """A config typo must not read as a working install with a different vendor."""
    settings = _health_settings(client)
    monkeypatch.setattr(settings, "llm_base_url", "http://127.0.0.1:1/v1", raising=False)
    monkeypatch.setattr(settings, "llm_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "ai_provider", "mixtral", raising=False)
    body = client.get("/health").json()
    assert "refused" in body["ai_provider"], body["ai_provider"]
    assert "mixtral" in body["ai_provider"]


# ---------------------------------------------------------------------------
# 6. a reply must not name a model that never answered
# ---------------------------------------------------------------------------


def _arm(core, monkeypatch, **updates):
    """A core whose settings point at whichever vendor the test is about."""
    from jarvis.config import get_settings

    settings = get_settings()
    for key, value in updates.items():
        monkeypatch.setattr(settings, key, value, raising=False)
    monkeypatch.setattr(core, "settings", settings, raising=False)
    from jarvis.gateway.router import ModelRouter

    monkeypatch.setattr(core, "router", ModelRouter(settings=settings))
    return settings


def test_a_provider_name_that_cannot_be_used_is_not_reported_as_an_answer(core, monkeypatch):
    """Found by running the live proof, not by reading the code.

    With `JARVIS_AI_PROVIDER=mixtral` the sentence still got its rule answer, and the response
    then said `model_used: "gemini-2.5-flash"`. A name that cannot be used means no call was
    made at all, so naming a model in the reply is a claim about a thing that did not happen --
    and the sentence "I also asked the model you configured" was the same lie in prose.
    """
    import asyncio

    from jarvis.core.models import CommandRequest

    _arm(core, monkeypatch, ai_provider="mixtral", llm_base_url="http://127.0.0.1:1/v1",
         llm_api_key="k", llm_model="gemini-2.5-flash")
    response = asyncio.run(core.handle(CommandRequest(
        text="wobbleth-and-glintar please", source="phase60")))

    assert response.model_used == "rules", response.model_used
    assert "mixtral" in response.reply, response.reply
    assert "did not ask" in response.reply or "cannot talk to" in response.reply, response.reply
    assert "said:" not in response.reply, response.reply


def test_an_unreachable_vendor_still_leaves_the_answer_attributed_to_the_rules(core, monkeypatch):
    """The same rule for the ordinary failure: the model was asked and did not answer, so the
    words the person read came from the rule router, and `model_used` has to say so."""
    import asyncio

    from jarvis.core.models import CommandRequest

    _arm(core, monkeypatch, ai_provider="openai", llm_base_url="http://127.0.0.1:1/v1",
         llm_api_key="k", llm_model="gpt-4o-mini")
    response = asyncio.run(core.handle(CommandRequest(
        text="wobbleth-and-glintar please", source="phase60")))

    assert response.model_used == "rules", response.model_used
    assert "unavailable" in response.reply, response.reply
