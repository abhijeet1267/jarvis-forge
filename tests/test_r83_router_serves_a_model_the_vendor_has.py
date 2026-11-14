"""The router must not send a model the configured vendor cannot serve.

**The model-planner path was dead, and it looked fine.**

`llm_fast_model` and `llm_reasoning_model` default to OpenAI names
(``gpt-4o-mini`` and ``o4-mini``). They are *hints* about which model suits
which kind of task, not a description of what the configured vendor offers.
An install that points at 9Router -- a local OpenAI-compatible gateway -- kept
those defaults, so the router sent ``o4-mini`` to a gateway that has never
heard of it:

    {"error":{"message":"No active credentials for provider: openai",
              "code":"model_not_found"}}

The failure was invisible on the conversational path, because ``conversation``
routes to ``general`` -- the one role whose model *is* the configured one. The
planner routes to ``reasoning``, so **every model-proposed action died at the
first request**. Measured, before the fix:

    POST /command "set a reminder for tomorrow at 9am to stretch my hamstrings"
      -> reply:    "I can set that up for you! I'll use the **Reminders app** ..."
      -> decision: None
      -> plan.planner: "rules"
      -> actions:  []
      -> pending confirmations: 0

The assistant promised an action, the user said "yes please go ahead", and
nothing happened and nothing was asked for approval. The conversation looked
perfect, which is exactly why the defect survived.

After the fix the same request produces a proposal and a held-for-approval
action:

      -> decision: CONFIRM
      -> needs_confirmation: True
      -> plan.planner: llm

Two rules are pinned here:

1. **A role model the vendor cannot serve is never called.** The vendor's own
   catalogue is used when it publishes one; when it does not (9Router, most
   compatible gateways), the route-health ledger's own record of what that
   gateway already refused is the evidence.
2. **A refusal of the model name is retried once on the configured model.** The
   first attempt provably never reached a model -- it was refused at the
   routing layer -- so nothing was generated, billed or persisted, and there is
   no double-answer risk. A timeout or a rate limit is *not* retried on another
   model: that is a statement about the route at that moment, and retrying a
   different model would hide a transient failure behind a different bill.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from jarvis.gateway.providers import ProviderReply  # noqa: E402
from jarvis.gateway.route_health import reset_route_health  # noqa: E402
from jarvis.gateway.router import ModelRouter  # noqa: E402


class _Settings:
    """The shape the router reads, with the values an install actually has."""

    def __init__(self, provider: str = "9router", model: str = "ag/gemini-2.5-flash"):
        self.ai_provider = provider
        self.llm_model = model
        self.llm_fast_model = "gpt-4o-mini"
        self.llm_reasoning_model = "o4-mini"
        self.llm_base_url = "http://127.0.0.1:20128/v1"
        self.llm_api_key = "test-key-not-a-secret"
        self.llm_enabled = True
        self.home = Path("/tmp/vantage-router-test")


class _Provider:
    """A provider that refuses every model, the way 9Router refuses `o4-mini`."""

    name = "9router"
    SUPPORTED_MODELS: list = []          # a gateway publishes no static catalogue
    DEFAULT_BASE_URL = "http://127.0.0.1:20128/v1"

    def __init__(self, settings=None):
        self.settings = settings or _Settings()
        self.calls: list = []

    async def chat(self, system, user, *, model, json_mode=False, timeout=30.0, history=()):
        self.calls.append(model)
        if model in ("ag/gemini-2.5-flash",):
            return ProviderReply(True, self.name, model, text='{"kind":"answer","text":"hi"}')
        return ProviderReply(
            False, self.name, model,
            error="provider returned 404: No active credentials for provider: openai",
            status=404,
            notes={"category": "model_not_found"},
        )


class _CatalogueProvider(_Provider):
    """A provider that publishes a catalogue, like OpenAI or Groq."""

    name = "groq"
    SUPPORTED_MODELS = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]

    def __init__(self, settings=None):
        super().__init__(settings)
        self.name = "groq"

    async def chat(self, system, user, *, model, json_mode=False, timeout=30.0, history=()):
        self.calls.append(model)
        if model in self.SUPPORTED_MODELS:
            return ProviderReply(True, self.name, model, text='{"kind":"answer","text":"hi"}')
        return ProviderReply(False, self.name, model, error="model not found", status=404,
                             notes={"category": "model_not_found"})


def _router(provider, settings=None):
    router = ModelRouter(settings or _Settings())
    router.__dict__["_test_provider"] = provider
    # The router resolves its provider through `select_provider(self.settings)`.
    # Patch the property for the duration of the test.
    type(router).provider = property(lambda self: self.__dict__["_test_provider"])
    return router


@pytest.fixture(autouse=True)
def _clean_ledger():
    reset_route_health()
    yield
    reset_route_health()


# ---------------------------------------------------------------------------
# 1. A model the vendor cannot serve is never called
# ---------------------------------------------------------------------------
def test_a_role_model_the_vendor_cannot_serve_is_not_called():
    """A vendor with a catalogue is asked only for models it publishes.

    `gpt-4o-mini` is an OpenAI name; Groq does not serve it. The router must
    not send it.
    """
    settings = _Settings(provider="groq", model="llama-3.3-70b-versatile")
    provider = _CatalogueProvider(settings)
    router = _router(provider, settings)

    choice = router.route("simple_command")   # role "fast" -> llm_fast_model

    assert choice.model in provider.SUPPORTED_MODELS, (
        f"the router asked for {choice.model!r}, which {provider.name} does "
        f"not serve: {provider.SUPPORTED_MODELS}")


def test_the_configured_model_answers_a_role_the_vendor_cannot_serve_specifically():
    """The operator's own model is the safe answer for an unservable role.

    It is the model they chose *for this provider*, so it is the one the
    provider can be expected to answer.
    """
    settings = _Settings(provider="groq", model="llama-3.1-8b-instant")
    provider = _CatalogueProvider(settings)
    router = _router(provider, settings)

    choice = router.route("planning")   # role "reasoning" -> llm_reasoning_model

    assert choice.model == "llama-3.1-8b-instant", (
        "the role model was used even though the vendor does not serve it")
    assert "cannot serve" in choice.reason, (
        "the substitution was not recorded in the routing reason")


# ---------------------------------------------------------------------------
# 2. A refusal of the model name is retried once on the configured model
# ---------------------------------------------------------------------------
def test_a_refused_model_is_retried_on_the_configured_model():
    """The planner's call is rescued instead of failing the turn.

    This is the exact shape of the live failure: the first call is refused at
    the routing layer (404 model_not_found), the second uses the model the
    gateway does serve.
    """
    settings = _Settings()
    provider = _Provider(settings)
    router = _router(provider, settings)

    async def go():
        return await router.complete_json("Return JSON.", "Return {\"kind\":\"answer\"}",
                                          task_type="planning", timeout=5)

    raw = asyncio.run(go())

    assert raw == {"kind": "answer", "text": "hi"}, (
        f"the planner's call did not recover: {raw!r}")
    assert provider.calls == ["o4-mini", "ag/gemini-2.5-flash"], (
        f"unexpected call sequence: {provider.calls}")


def test_a_timeout_is_not_retried_on_another_model():
    """A timeout is a statement about the route, not about the model's name.

    Retrying a different model would hide a transient failure behind a
    different bill -- and would spend a second call the first one did not
    justify.
    """
    settings = _Settings()
    provider = _Provider(settings)

    async def chat(system, user, *, model, json_mode=False, timeout=30.0, history=()):
        provider.calls.append(model)
        return ProviderReply(False, provider.name, model, error="timeout: timed out",
                             notes={"category": "timeout"})

    provider.chat = chat
    router = _router(provider, settings)

    async def go():
        return await router.complete_json("Return JSON.", "Return {\"kind\":\"answer\"}",
                                          task_type="planning", timeout=5)

    raw = asyncio.run(go())

    assert raw is None, "a timeout must not be reported as an answer"
    assert provider.calls == ["o4-mini"], (
        f"a timeout was retried on another model: {provider.calls}")


def test_a_successful_call_is_not_retried():
    """The ordinary path is untouched: one call, one answer."""
    settings = _Settings()
    provider = _Provider(settings)
    router = _router(provider, settings)

    async def go():
        return await router.complete_json("Return JSON.", "Return {\"kind\":\"answer\"}",
                                          task_type="conversation", timeout=5)

    raw = asyncio.run(go())

    assert raw == {"kind": "answer", "text": "hi"}
    assert provider.calls == ["ag/gemini-2.5-flash"], (
        f"a working call was repeated: {provider.calls}")


# ---------------------------------------------------------------------------
# 3. The ledger is the evidence for a gateway with no catalogue
# ---------------------------------------------------------------------------
def test_a_gateway_that_already_refused_a_model_is_not_asked_again():
    """9Router publishes no catalogue, so the ledger is the only evidence.

    Once the gateway has said "model not found" for a name, asking again
    spends a round trip to learn the same thing. The configured model answers
    instead.
    """
    from jarvis.gateway.route_health import get_route_health

    settings = _Settings()
    provider = _Provider(settings)
    router = _router(provider, settings)

    get_route_health().record("9router", "o4-mini", ok=False, source="chat",
                              status=404, category="model_not_found",
                              error="No active credentials for provider: openai")

    choice = router.route("planning")

    assert choice.model == "ag/gemini-2.5-flash", (
        f"the router asked again for a model the gateway already refused: "
        f"{choice.model!r}")


def test_a_later_success_overrules_an_earlier_refusal():
    """A name that worked after a refusal is not treated as dead.

    The gateway's catalogue changes; a model that was withdrawn can come back.
    """
    from jarvis.gateway.route_health import get_route_health

    settings = _Settings()
    provider = _Provider(settings)
    router = _router(provider, settings)

    ledger = get_route_health()
    ledger.record("9router", "o4-mini", ok=False, source="chat", status=404,
                  category="model_not_found", error="no")
    ledger.record("9router", "o4-mini", ok=True, source="chat")

    choice = router.route("planning")

    assert choice.model == "o4-mini", (
        "a model that succeeded after a refusal was treated as dead")


def test_a_flaky_route_is_not_treated_as_a_dead_model_name():
    """A timeout is not evidence that the *name* is wrong.

    Only `model_not_found` counts. A rate-limited or slow route says nothing
    about the model's name, and treating it as one would silently downgrade a
    working model.
    """
    from jarvis.gateway.route_health import get_route_health

    settings = _Settings()
    provider = _Provider(settings)
    router = _router(provider, settings)

    get_route_health().record("9router", "o4-mini", ok=False, source="chat",
                              status=429, category="rate_limited", error="slow down")

    choice = router.route("planning")

    assert choice.model == "o4-mini", (
        "a rate-limited route was treated as a dead model name")


# ---------------------------------------------------------------------------
# 4. The live configuration this defect was found in
# ---------------------------------------------------------------------------
def test_the_live_install_does_not_ship_a_dead_planner_path():
    """The installed configuration must have a working planning route.

    This is the check that would have caught the defect on the machine it
    happened on: the configured provider is asked for a model it can serve.
    """
    from jarvis.config import get_settings
    from jarvis.gateway.providers import PROVIDERS, select_provider

    settings = get_settings()
    if not settings.llm_enabled:
        pytest.skip("no model endpoint is configured on this machine")

    provider = select_provider(settings)
    configured = str(settings.llm_model or "").strip()
    assert configured, "the install has a model endpoint but no model id"

    offered = getattr(provider, "SUPPORTED_MODELS", None) or []
    if offered:
        assert configured in offered, (
            f"the configured model {configured!r} is not one of {provider.name}'s: "
            f"{offered}")

    # The role models are hints; each must either be servable or be replaceable
    # by the configured model.
    for role_model in (settings.llm_fast_model, settings.llm_reasoning_model):
        if offered and role_model not in offered:
            assert configured in offered, (
                f"role model {role_model!r} is not servable and the configured "
                f"model {configured!r} is not either -- every role would fail")
